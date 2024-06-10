import contextlib
import fnmatch
import glob
import inspect
import json
import logging
import os
from typing import Any, Collection, Dict, Mapping, Sequence

from morphocut.tiles import TiledPipeline
import natsort
import numpy as np
import pydantic
import skimage
import torch
import torchvision.transforms.functional as tvtf
from morphocut.batch import BatchedPipeline
from morphocut.contrib.ecotaxa import EcotaxaReader, EcotaxaWriter
from morphocut.core import Call, Pipeline, StreamObject, Variable
from morphocut.hdf5 import HDF5Writer
from morphocut.pipelines import DataParallelPipeline
from morphocut.stream import Progress, Slice, Unpack
from morphocut.torch import PyTorch

from skimage.measure import regionprops

from ..common import convert_img_dtype
from ..pipeline_runner import PipelineRunner
from .config_schema import ModelMetaSchema, PredictionPipelineConfig

logging.captureWarnings(True)
logger = logging.getLogger(__name__)


def _find_files_glob(pattern: str, ignore_patterns: Collection | None = None):
    for fn in glob.iglob(pattern):
        if ignore_patterns is not None and any(
            fnmatch.fnmatch(fn, pat) for pat in ignore_patterns
        ):
            logger.info(f"Ignoring {fn}.")
            continue

        yield fn


def measure_segments(
    object_id: str, predictions: np.ndarray, channels: Sequence[str]
) -> Mapping[str, Any]:
    result: Dict[str, Any] = {"object_id": object_id}

    # Make sure that predictions has the expected dtype and shape
    # assert np.issubdtype(
    #     predictions.dtype, np.integer
    # ), f"Expected integer, got {predictions.dtype}"
    predictions = predictions.astype(int)
    assert predictions.ndim == 3
    assert predictions.shape[0] == len(channels)

    for j, name in enumerate(channels):
        area_predicted = predictions[j].sum()

        result[f"{name}_area"] = area_predicted

        try:
            (props_predicted,) = regionprops(predictions[j])
        except ValueError:
            result[f"{name}_major_length"] = 0
        else:
            result[f"{name}_major_length"] = props_predicted.axis_major_length

    return result


class Runner(PipelineRunner):
    @staticmethod
    def _configure_and_run(config_dict):
        try:
            config = PredictionPipelineConfig.model_validate(config_dict)
        except pydantic.ValidationError as exc:
            logger.error(str(exc))
            return

        with Pipeline() as p:
            process_meta_var = Variable("process_meta", p)
            process_meta = {}

            # Discover input archives
            input_archive_fns = list(
                _find_files_glob(config.input.path, config.input.ignore_patterns)
            )

            logger.info(
                f"Found {len(input_archive_fns):d} input archives in {config.input.path}"
            )

            input_archive_fn = Unpack(
                natsort.natsorted(
                    input_archive_fns, alg=natsort.PATH | natsort.IGNORECASE
                )
            )

            Progress(input_archive_fn)

            predictions_fn = Call(
                lambda input_archive_fn: os.path.join(
                    config.target_dir,
                    os.path.splitext(os.path.basename(input_archive_fn))[0] + ".h5",
                ),
                input_archive_fn,
            )

            measurements_fn = Call(
                lambda input_archive_fn: os.path.join(
                    config.target_dir,
                    os.path.splitext(os.path.basename(input_archive_fn))[0] + ".zip",
                ),
                input_archive_fn,
            )

            et_obj = EcotaxaReader(
                input_archive_fn,
                # query: RawOrVariable[Optional[str]] = None,
                # prepare_data: Optional[Callable[["pd.DataFrame"], "pd.DataFrame"]] = None,
                # verbose=False,
                # keep_going=False,
                # print_summary=False,
                # encoding="utf-8",
                # index_pattern="*ecotaxa_*",
                # columns: Optional[List] = None,
                # image_default_mode=None,
            )

            image = et_obj.image
            object_id = Call(lambda et_obj: et_obj.meta["object_id"], et_obj)

            if config.input.max_n_objects is not None:
                Slice(config.input.max_n_objects)

            Progress(object_id)

            ###

            extra_files = {"meta.json": None}
            model: torch.jit.ScriptModule = torch.jit.load(
                config.model.model_fn,
                map_location=config.model.device,
                _extra_files=extra_files,
            )
            model_meta_dict: Dict = (
                json.loads(extra_files["meta.json"]) if extra_files["meta.json"] else {}
            )

            # Merge config meta into model meta
            if config.model.meta is not None:
                model_meta_dict.update(config.model.meta.model_dump())

            model_meta = ModelMetaSchema.model_validate(model_meta_dict)

            ((input_name, input_description),) = model_meta.inputs.items()
            ((output_name, output_description),) = model_meta.outputs.items()

            logger.info(model.code)
            logger.info(f"Input channels '{input_name}': {input_description.channels}")
            logger.info(
                f"Output channels '{output_name}': {output_description.channels}"
            )

            # Convert model to the specified dtype
            torch_dtype = getattr(torch, config.model.dtype)
            np_dtype = np.dtype(config.model.dtype)
            model = model.to(torch_dtype)

            def pre_transform(img: np.ndarray) -> torch.Tensor:
                """Ensure RGB image, convert to specified dtype and transpose."""
                if img.ndim == 2:
                    img = skimage.color.gray2rgb(img)

                img = img.transpose((2, 0, 1))

                img = convert_img_dtype(img, np_dtype)

                tensor = torch.from_numpy(img)

                # Extract center 1024x1024 window (or pad)
                tensor = tvtf.center_crop(tensor, 1024)

                return tensor.contiguous()

            with contextlib.ExitStack() as maybe_batched:
                if config.model.batch_size:
                    maybe_batched.enter_context(
                        BatchedPipeline(config.model.batch_size)
                    )
                    is_batch = True
                else:
                    is_batch = False

                with contextlib.ExitStack() as model_stack:
                    # if config.model.tiled:
                    #     model_stack.enter_context(
                    #         TiledPipeline((1024, 1024), image, tile_stride=(896, 896))
                    #     )

                    if config.model.n_threads > 1:
                        model_stack.enter_context(
                            DataParallelPipeline(executor=config.model.n_threads)
                        )

                    predictions = PyTorch(
                        model,
                        image,
                        device=config.model.device,
                        is_batch=is_batch,
                        # output_key=config.model.output_key,
                        # pin_memory=None,
                        pre_transform=pre_transform,
                        # post_transform: Optional[Callable] = None,
                        # autocast=False,
                    )

                if config.save_raw_predictions:
                    HDF5Writer(
                        predictions_fn,
                        {
                            "object_id": object_id,
                            "predictions": predictions,
                        },
                        dataset_mode="extend" if is_batch else "append",
                    )

            if config.measure_segments:
                meta = Call(
                    measure_segments,
                    object_id,
                    predictions,
                    output_description.channels,
                )

                EcotaxaWriter(measurements_fn, [], meta=meta)

        # Inject pipeline metadata into the stream
        obj = StreamObject(n_remaining_hint=1)
        obj[process_meta_var] = process_meta
        p.run(iter([obj]))

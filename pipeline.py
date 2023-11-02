import contextlib
import datetime
import glob
import gzip
import logging
import os
import pathlib
import pickle
import traceback
from typing import IO, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

import morphocut
import numpy as np
import pandas as pd
import parse
import skimage.color
import skimage.exposure
import skimage.filters
import skimage.measure
import skimage.morphology
import yaml
from maze_dl.merge_labels import merge_labels
from morphocut import Pipeline
from morphocut.batch import BatchedPipeline
from morphocut.contrib.ecotaxa import EcotaxaWriter
from morphocut.contrib.zooprocess import CalculateZooProcessFeatures
from morphocut.core import (
    Call,
    Node,
    RawOrVariable,
    Stream,
    Variable,
    closing_if_closable,
)
from morphocut.file import Glob
from morphocut.image import ExtractROI, FindRegions, ImageProperties, ImageReader
from morphocut.pipelines import (
    AggregateErrorsPipeline,
    DataParallelPipeline,
    MergeNodesPipeline,
)
from morphocut.scalebar import DrawScalebar
from morphocut.stitch import Stitch
from morphocut.stream import Filter, Progress, Slice, StreamBuffer, Unpack
from morphocut.tiles import TiledPipeline
from morphocut.torch import PyTorch
from skimage.feature.orb import ORB
from skimage.measure._regionprops import RegionProperties
from tqdm import tqdm
from zoomie2 import DetectDuplicatesSimple
from omni_archive import Archive

import _version
import lokidata
import segmenter


logging.captureWarnings(True)
logger = logging.getLogger(__name__)


class _PathLike(Protocol):
    def __truediv__(self, k) -> "_PathLike":
        ...

    def open(self) -> IO:
        ...

    def glob(self, pattern: str) -> Iterable["_PathLike"]:
        ...

    @property
    def suffix(self) -> str:
        ...

class FilterEval(Node):
    """
    Filter the stream using a boolean expression.
    """

    def __init__(self, expression: str, data: RawOrVariable[Mapping]):
        super().__init__()

        self._compiled_expr = compile(expression, "<string>", "eval")

        self.data = data

    def transform_stream(self, stream: Stream) -> Stream:
        with closing_if_closable(stream):
            for obj in stream:
                try:
                    data: Mapping = self.prepare_input(obj, "data") # type: ignore

                    if eval(self._compiled_expr, None, data):
                        yield obj
                except Exception as exc:
                    raise type(exc)(*exc.args, f"{self}")


def read_log_and_yaml_meta(data_root: _PathLike, meta: Mapping):
    # Find singular log filename
    (log_fn,) = (data_root / "Log").glob("LOKI*.log")
    meta_fn = data_root / "meta.yaml"

    # Return combination of initial meta, LOKI log metadata and yaml meta
    return {
        **meta,
        **lokidata.read_log(log_fn, format="ecotaxa"),
        **lokidata.read_yaml(meta_fn),
    }


TMD2META = {
    "object_longitude": "GPS_LON",
    "object_latitude": "GPS_LAT",
    "object_pressure": "PRESS",
    "object_temperature": "TEMP",  # or OXY_TEMP?
    "object_oxygen_concentration": "OXY_CON",
    "object_oxygen_saturation": "OXY_SAT",
    "object_temperature_oxsens": "OXY_TEMP",
    "object_conductivity": "COND_COND",
    # "COND_TEMP",
    "object_salinity": "COND_SALY",
    # "COND_DENS",
    # "COND_SSPEED"
    # "FLOUR_1",
    # "FLOUR_CR",
    # "FLOUR_CV",
    # "FLOUR_TR",
    # "FLOUR_TD",
    # "ROLL",
    # "PITCH",
    # "NICK",
    # "LOKI_REC",
    # "LOKI_PIC",
    # "LOKI_FRAME",
    # "CAM_STAT",
    # "HOUSE_STAT",
    # "HOUSE_T1",
    # "HOUSE_T2",
    # "HOUSE_VOLT",
}

tmd_fn_pat = "{:04d}{:02d}{:02d} {:02d}{:02d}{:02d}"
telemetry_fn_parser = parse.compile(tmd_fn_pat)


def parse_telemetry_fn(tmd_fn: _PathLike):
    r: parse.Result = telemetry_fn_parser.search(tmd_fn.name)  # type: ignore
    if r is None:
        raise ValueError(f"Could not parse telemetry filename: {tmd_fn.name}")

    return datetime.datetime(*r)


def _read_tmd(tmd_fn: _PathLike, ignore_errors=False):
    dt = parse_telemetry_fn(tmd_fn)

    try:
        tmd = lokidata.read_tmd(tmd_fn)
    except:
        logger.error(f"Error reading {tmd_fn}", exc_info=True)
        if not ignore_errors:
            raise
        return dt, {}

    return dt, {k_et: tmd[k_loki] for k_et, k_loki in TMD2META.items() if k_loki in tmd}


def _read_dat(dat_fn: _PathLike, ignore_errors=False):
    dt = parse_telemetry_fn(dat_fn)

    try:
        dat = lokidata.read_dat(dat_fn)
    except Exception:
        logger.error(f"Error reading {dat_fn}", exc_info=True)
        if not ignore_errors:
            raise
        return dt, {}

    return dt, {k_et: dat[k_loki] for k_et, k_loki in TMD2META.items() if k_loki in dat}


def read_all_telemetry(data_root: _PathLike, ignore_errors=False):
    logger.info("Reading telemetry...")

    tmd_fns = (data_root / "Telemetrie").glob("*.tmd")
    tmd_names = set(tmd_fn.stem for tmd_fn in tmd_fns)

    telemetry = pd.DataFrame.from_dict(
        dict(
            _read_tmd(tmd_fn, ignore_errors=ignore_errors)
            for tmd_fn in tqdm(tmd_fns, desc=f"{data_root}/*.tmd")
        ),
        orient="index",
    )

    # Also read .dat files
    dat_fns = (data_root / "Telemetrie").glob("*.dat")
    dat = pd.DataFrame.from_dict(
        dict(
            _read_dat(dat_fn, ignore_errors=ignore_errors)
            for dat_fn in tqdm(dat_fns, desc=f"{data_root}/*.dat")
            if dat_fn.stem not in tmd_names
        ),
        orient="index",
    )
    dat = dat[~dat.index.isin(telemetry.index)]
    if not dat.empty:
        telemetry = pd.concat((telemetry, dat))

    return telemetry.sort_index()


def merge_telemetry(meta: Dict, telemetry: pd.DataFrame):
    # Construct tmd_fn and extract date
    tmd_fn = "{object_date} {object_time}.tmd".format_map(meta)
    dt = parse_telemetry_fn(pathlib.PurePosixPath(tmd_fn))

    (idx,) = telemetry.index.get_indexer([dt], method="nearest")
    return {**meta, **telemetry.iloc[idx].to_dict()}


REQUIRED_SAMPLE_META = [
    "sample_bottomdepth",
    "sample_region",
    "sample_detail_location",
    "sample_vessel",
    "sample_latitude",
    "sample_longitude",
    "sample_station",
    "sample_haul",
    "acq_instrument",
]


class MissingMetaError(Exception):
    pass


def update_and_validate_sample_meta(data_root: str, meta: Dict):
    """
    Validate metadata.

    Make sure that all required fields are included and generate sample_id and acq_id.
    """

    missing_keys = set(REQUIRED_SAMPLE_META) - set(meta.keys())
    if missing_keys:
        missing_keys_str = ", ".join(sorted(missing_keys))

        raise MissingMetaError(
            f"The following fields are missing: {missing_keys_str}.\nSupply them in {meta_fn}"
        )

    meta["sample_id"] = "{sample_station}_{sample_haul}".format_map(meta)
    meta["acq_id"] = "{acq_instrument}_{sample_id}".format_map(meta)

    meta["process_datetime"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta["process_id"] = "{acq_id}_{process_datetime}".format_map(meta)
    meta["process_morphocut_version"] = morphocut.__version__
    meta["process_loki_pipeline_version"] = _version.get_versions()["version"]
    # TODO: More process metadata

    return meta


objid_pattern = "{object_date} {object_time}  {object_milliseconds}  {object_sequence:06d} {object_posx:04d} {object_posy:04d}"
objid_parser = parse.compile(objid_pattern)


def parse_object_id(object_id, meta):
    result = objid_parser.parse(object_id)
    if result is None:
        raise ValueError(f"Can not parse object ID: {object_id}")

    object_frame_id = "{object_date} {object_time}  {object_milliseconds}".format_map(
        result.named
    )

    return {
        **meta,
        "object_id": object_id,
        "object_frame_id": object_frame_id,
        **result.named,
    }


def stretch_contrast(image, low, high):
    low_, high_ = np.quantile(image, (low, high))
    return skimage.exposure.rescale_intensity(image, (low_, high_))


def build_stored_segmentation(stored_config, image, meta):
    pickle_fn = stored_config["pickle_fn"]

    with gzip.open(pickle_fn, "rb") as f:
        segmenter_: segmenter.Segmenter = pickle.load(f)
    segmenter_.configure(n_jobs=1, verbose=0)

    def _predict_scores(image, segmenter_):
        features = segmenter_.extract_features(image)
        mask = segmenter_.preselect(image)
        scores = segmenter_.predict_pixels(features, mask)

        return scores

    scores = Call(_predict_scores, image, segmenter_)

    image_large, scores_large = Stitch(
        image,
        scores,
        groupby=meta["object_frame_id"],
        offset=(meta["object_posy"], meta["object_posx"]),
    )

    # TODO: Remove recurring artefacts by looking at neighboring frames: Drop region if covered (more than x%) by more than N regions in M neighboring frames.

    if stored_config["skip_single"]:
        # DEBUG: Keep only frames with multiple regions
        keep = Call(lambda image_large: image_large.n_regions > 1, image_large)
        Filter(keep)

    labels_large = Call(segmenter_.postprocess, scores_large, image_large)

    if stored_config["full_frame_archive_fn"] is not None:
        segment_image = Call(
            lambda labels, image: skimage.util.img_as_ubyte(
                skimage.color.label2rgb(labels, image, bg_label=0, bg_color=None)
            ),
            labels_large,
            image_large,
        )

        score_image = Call(
            skimage.util.img_as_ubyte,
            scores_large,
        )

        full_frame_archive_fn = Call(
            str.format_map, stored_config["full_frame_archive_fn"], meta
        )

        #  Store stitched result
        EcotaxaWriter(
            full_frame_archive_fn,
            [
                # Input image
                ("img/" + meta["object_frame_id"] + ".png", image_large),
                # Image overlayed with labeled segments
                ("overlay/" + meta["object_frame_id"] + ".png", segment_image),
                ("score/" + meta["object_frame_id"] + ".png", score_image),
                # # Mask (255=foreground)
                # (
                #     "mask/" + meta["object_frame_id"] + ".png",
                #     Call(lambda labels: (labels > 0).astype("uint8")*255, labels_large),
                # ),
            ],
        )
    
    # Extract individual objects
    region = FindRegions(labels_large, image_large, padding=75)


    image = ExtractROI(
        image,
        region,
        alpha=1 if stored_config["apply_mask"] else 0,
        bg_color=0,
    )

    def recalc_metadata(region: RegionProperties, meta):
        meta = meta.copy()

        (y0, x0, x1, y1) = region.bbox

        meta["object_posx"] = x0
        meta["object_posy"] = y0
        meta["object_sequence"] = region.label
        meta["object_width"] = x1 - x0
        meta["object_height"] = y1 - y0

        meta["object_id"] = objid_pattern.format_map(meta)

        return meta

    # Re-calculate metadata (object_id, posx, posy)
    meta = Call(recalc_metadata, region, meta)

    # Re-calculate features
    meta = CalculateZooProcessFeatures(region, meta, prefix="object_")
    return image, meta


def assert_compatible_shape(label, image):
    try:
        assert (
            image.shape[: label.ndim] == label.shape
        ), f"{label.shape} vs. {image.shape}"
    except:
        logger.error(f"{label.shape} vs. {image.shape}", exc_info=True)
        raise


def convert_img_dtype(image, dtype: np.dtype):
    image = np.asarray(image)

    if dtype.kind == "f":
        if image.dtype.kind == "u":
            factor = np.array(1.0 / np.iinfo(image.dtype).max, dtype=dtype)
            return np.multiply(image, factor)

        if image.dtype.kind == "f":
            return np.asarray(image, dtype)

    raise ValueError(f"Can not convert {image.dtype} to {dtype}.")


def build_segmentation_postprocessing(config: Mapping, foreground_pred):
    with contextlib.ExitStack() as exit_stack:
        if config["n_threads"] > 1:
            # Post-processing task is CPU-bound
            exit_stack.enter_context(DataParallelPipeline(executor=config["n_threads"]))

        # Convert to bool
        foreground_pred = Call(np.asarray, foreground_pred, dtype=bool)

        # Opening (remove small details)
        if config["opening_radius"] > 0:
            foreground_pred = Call(
                skimage.morphology.binary_opening,
                foreground_pred,
                skimage.morphology.disk(config["opening_radius"]),
            )

        # Closing (close small gaps)
        if config["closing_radius"] > 0:
            foreground_pred = Call(
                skimage.morphology.binary_closing,
                foreground_pred,
                skimage.morphology.disk(config["closing_radius"]),
            )

        # Label the image
        labels = Call(
            skimage.measure.label,
            foreground_pred,
        )

        if config["clear_border"]:
            Call(lambda labels: skimage.segmentation.clear_border(labels, out=labels), labels)

        # Remove objects below area threshold
        if config["min_area"] > 0:
            labels = Call(
                skimage.morphology.remove_small_objects,
                labels,
                min_size=config["min_area"],
                out=labels,
            )

        # Merge neighboring labels
        if config["merge_labels"] > 0:
            labels = Call(
                merge_labels,
                labels,
                max_distance=config["merge_labels"],
                labels_out=labels,
            )

    return foreground_pred, labels


def build_pytorch_segmentation(config: Mapping, image: Variable[np.ndarray], meta):
    import torch
    import torch.jit

    # # Construct image AnchoredArray (with slices meta-information)
    # image = Call(
    #     lambda image, x, y: AnchoredArray.create(image, offsets=(y, x)),
    #     image,
    #     meta["object_posx"],
    #     meta["object_posy"],
    # )
    # if config["cluster_stitch"]:
    #     # Buffer to have enough ROIs immediately available for stitching
    #     StreamBuffer(16)
    #     region = ClusterStitch(region, groupby=meta["object_frame_id"], margin=75)

    if config["stitch"] is not None and config["stitch"]:
        # Buffer to have enough ROIs immediately available for stitching
        StreamBuffer(16)

        # Stitch individual ROIs together to build a full-frame image
        image = Stitch(
            image,
            groupby=meta["object_frame_id"],
            offset=(meta["object_posy"], meta["object_posx"]),
        )

        if config["stitch"]["skip_single"]:
            keep = Call(lambda image: image.n_regions > 1, image)
            Filter(keep)

    # Deep Learning Segmentation
    device = config["device"]
    dtype = config["dtype"]

    if config.get("model_fn"):
        model = torch.load(config.get("model_fn"), map_location=device)
    elif config.get("jit_model_fn"):
        model = torch.jit.load(config.get("jit_model_fn"), map_location=device)
    else:
        raise ValueError("No model fn")

    assert isinstance(model, torch.nn.Module), "Model is not a torch.nn.Module"

    # Convert model to the specified dtype
    torch_dtype = getattr(torch, dtype)
    np_dtype = np.dtype(dtype)
    model = model.to(torch_dtype)

    def pre_transform(img):
        """Ensure RGB image, convert to specified dtype and transpose."""
        if img.ndim == 2:
            img = skimage.color.gray2rgb(img)

        img = img.transpose((2, 0, 1))

        img = convert_img_dtype(img, np_dtype)

        return torch.from_numpy(img).contiguous()

    print("Image to tile", repr(image))
    with TiledPipeline((1024, 1024), image, tile_stride=(896, 896)):
        # # Skip empty tiles
        # Filter(lambda obj: (obj[image] > 0).any())
        # TODO: Rework Filter
        # Filter(lambda image: (image > 0).any(), image)
        Filter(Call(lambda image: (image > 0).any(), image))

        with contextlib.ExitStack() as exit_stack:
            if config["batch_size"]:
                exit_stack.enter_context(BatchedPipeline(config["batch_size"]))

            if config["n_threads"] > 1:
                exit_stack.enter_context(
                    DataParallelPipeline(executor=config["n_threads"])
                )

            foreground_pred = PyTorch(
                model,
                image,
                device=device,
                output_key=0,
                pre_transform=pre_transform,
                pin_memory=True,
                autocast=config["autocast"],
            )

            # # Dummy for speed optimization
            # foreground_pred = Call(
            #     lambda image: np.zeros(image.shape[:2], dtype=bool),
            #     image,
            # )

    # Postprocessing
    foreground_pred, labels = build_segmentation_postprocessing(
        config["postprocess"], foreground_pred
    )

    # DEBUG: Store segmentation output
    if config["full_frame_archive_fn"] is not None:
        Call(assert_compatible_shape, labels, image)
        segment_image = Call(
            lambda labels, image: skimage.util.img_as_ubyte(
                skimage.color.label2rgb(labels, image, bg_label=0, bg_color=None)
            ),
            labels,
            image,
        )

        score_image = Call(
            skimage.util.img_as_ubyte,
            foreground_pred,
        )

        full_frame_archive_fn = Call(
            str.format_map, config["full_frame_archive_fn"], meta
        )

        #  Store stitched result
        EcotaxaWriter(
            full_frame_archive_fn,
            [
                # Input image
                ("img/" + meta["object_frame_id"] + ".png", image),
                # Image overlayed with labeled segments
                ("overlay/" + meta["object_frame_id"] + ".png", segment_image),
                ("score/" + meta["object_frame_id"] + ".png", score_image),
            ],
        )

        StreamBuffer(2)

    # Extract individual objects
    region = FindRegions(
        labels, image, padding=75, min_intensity=config["min_intensity"]
    )

    image = ExtractROI(
        image, region, alpha=1 if config["apply_mask"] else 0, bg_color=config["background_color"]
    )

    def recalc_metadata(region: RegionProperties, meta):
        meta = meta.copy()

        (y0, x0, x1, y1) = region.bbox

        meta["object_posx"] = x0
        meta["object_posy"] = y0
        meta["object_sequence"] = region.label
        meta["object_width"] = x1 - x0
        meta["object_height"] = y1 - y0

        meta["object_id"] = objid_pattern.format_map(meta)

        return meta

    # Re-calculate metadata (object_id, posx, posy)
    meta = Call(recalc_metadata, region, meta)

    # Re-calculate features
    meta = CalculateZooProcessFeatures(region, meta, prefix="object_")

    # # Restore regular ndarray image
    # image = Call(lambda image: image.data, image)

    return image, meta, region.image


def one_of(
    config: Mapping, keys: Sequence
) -> Union[Tuple[None, None], Tuple[str, Mapping]]:
    matches = [k for k in keys if k in config]

    if not matches:
        return (None, None)

    if len(matches) == 1:
        k = matches[0]
        return (k, config[k])

    raise ValueError(f"Multiple matches for {keys} in {config}")


def build_threshold_segmentation(config, image, meta):
    mask = image > config["threshold"]

    Filter(lambda obj: obj[mask].any())

    props = ImageProperties(mask, image)
    meta = CalculateZooProcessFeatures(props, meta, prefix="object_")

    return image, meta


def build_segmentation(config, image, meta) -> Tuple[Variable, Variable, RawOrVariable]:
    mask = None

    if config is None:
        return image, meta, mask

    segmentation_type, segmentation_config = one_of(
        config, ["threshold", "stored", "pytorch"]
    )
    if segmentation_type is None:
        raise ValueError(f"No segmentation type specified")
    elif segmentation_type == "threshold":
        image, meta= build_threshold_segmentation(segmentation_config, image, meta)
    elif segmentation_type == "stored":
        image, meta=build_stored_segmentation(segmentation_config, image, meta)
    elif segmentation_type == "pytorch":
        image, meta, mask = build_pytorch_segmentation(segmentation_config, image, meta)
    else:
        raise ValueError(f"Unknown segmentation config: {config}")
    
    if config["filter_expr"] is not None:
        logger.info(f"Filtering output by expression {config['filter_expr']!r}")
        FilterEval(config["filter_expr"], meta)

    return image, meta, mask


def detector_extractor(img):
    # img = skimage.transform.rescale(img, 0.75, preserve_range=True)
    img = skimage.filters.gaussian(img, 0.5, preserve_range=True)

    detector_extractor = ORB(
        downscale=1.2,
        fast_n=12,
        fast_threshold=0.84,
        harris_k=0.02,
        n_keypoints=100,
        n_scales=8,
    )
    detector_extractor.detect_and_extract(img)

    return detector_extractor.keypoints, detector_extractor.descriptors


def calc_overlap(xy0, wh0, xy1, wh1):
    """overlap_xy OR (keypoint_score AND overlap_y)"""

    l0 = xy0[0]
    r0 = xy0[0] + wh0[0]

    l1 = xy1[0]
    r1 = xy1[0] + wh1[0]

    t0 = xy0[1]
    b0 = xy0[1] + wh0[1]

    t1 = xy1[1]
    b1 = xy1[1] + wh1[1]

    w0, h0 = wh0
    w1, h1 = wh0

    intersect_x = max(0, min(r0, r1) - max(l0, l1))
    intersect_y = max(0, min(b0, b1) - max(t0, t1))

    union_x = max(1, max(r0, r1) - min(l0, l1))
    union_y = max(1, max(b0, b1) - min(t0, t1))

    overlap_x = intersect_x / union_x
    overlap_y = intersect_y / union_y

    intersect_xy = intersect_x * intersect_y
    overlap_xy = intersect_xy / (w0 * h0 + w1 * h1 - intersect_xy)

    return overlap_x, overlap_y, overlap_xy


def score_fn_simple(meta0, meta1):
    xy0 = meta0["object_posx"], meta0["object_posy"]
    xy1 = meta1["object_posx"], meta1["object_posy"]
    wh0 = meta0["object_width"], meta0["object_height"]
    wh1 = meta1["object_width"], meta1["object_height"]

    overlap_x, overlap_y, overlap_xy = calc_overlap(xy0, wh0, xy1, wh1)

    return overlap_xy


def build_object_frame_id_filter(
    filter_object_frame_id_fn: str | None, meta: Variable[Mapping]
):
    if filter_object_frame_id_fn is None:
        return

    logger.info(f"Filtering object_frame_id from {filter_object_frame_id_fn}...")

    index = pd.Index(
        pd.read_csv(filter_object_frame_id_fn, header=None, index_col=False).squeeze()
    )

    Filter(lambda obj: obj[meta]["object_frame_id"] in index)

def build_input(input_config: Mapping, output_config: Mapping):
    meta = input_config.get("meta", {})

    # Set defaults
    meta.setdefault("acq_instrument", "LOKI")

    data_roots: List[_PathLike]
    if "discover" in input_config:
        # List directories that contain "Pictures" and "Telemetrie" folders
        data_roots = list(
            lokidata.find_data_roots(input_config["path"], input_config.get("ignore_patterns", None))
        )
    elif "glob" in input_config:
        data_roots = [Archive(fn) for fn in glob.glob(input_config["glob"]["pattern"])]  # type: ignore
    else:
        raise ValueError("Specify one of ''discover' or 'glob' in 'input'")

    logger.info(f"Found {len(data_roots):d} input directories")

    data_root = Unpack(data_roots)

    Progress(data_root)

    # Load LOG metadata
    meta = Call(read_log_and_yaml_meta, data_root, meta)

    # Preload all metadata (to avoid later validation errores)
    with AggregateErrorsPipeline():
        # Load additional metadata
        meta = Call(update_and_validate_sample_meta, data_root, meta)

    if input_config["merge_telemetry"]:
        # Load *all* telemetry data
        telemetry = Call(read_all_telemetry, data_root, ignore_errors=True)
    else:
        telemetry = None

    os.makedirs(output_config["path"], exist_ok=True)

    target_archive_fn = Call(
        lambda meta: os.path.join(
            output_config["path"],
            "LOKI_{sample_station}_{sample_haul}.zip".format_map(meta),
        ),
        meta,
    )

    if input_config["save_meta"]:
        input_meta_archive_fn = Call(
            lambda meta: os.path.join(
                output_config["path"],
                "LOKI_{sample_station}_{sample_haul}_input_meta.zip".format_map(
                    meta
                ),
            ),
            meta,
        )

    # Buffer per-data_root processing (telemetry, metadata)
    # StreamBuffer(1)

    Call(print, meta, "\n")

    # Find images
    picture_fns = Call(
        lambda data_root: sorted((data_root / "Pictures").glob("*/*.bmp")),
        data_root,
    )
    picture_fn = Unpack(picture_fns)

    # Extract object ID
    object_id = Call(
        lambda picture_fn: picture_fn.stem,
        picture_fn,
    )

    # Parse object ID and update metadata
    meta = Call(parse_object_id, object_id, meta)

    # Skip frames where no annotated objects exist (if configured)
    build_object_frame_id_filter(input_config["filter_object_frame_id"], meta)

    # DEBUG: Slice
    if input_config["slice"] is not None:
        logger.warning(f"Only processing the first {input_config['slice']} input objects.")
        Slice(input_config["slice"])

    def error_handler(exc, img_fn):
        traceback.print_exc(file=sys.stderr)
        logger.error(f"Could not read image: {img_fn}", exc_info=True)

    # Skip object if image can not be loaded
    with MergeNodesPipeline(on_error=error_handler, on_error_args=(picture_fn,)):
        # Read image
        image = ImageReader(picture_fn, "L")

    meta = Call(
        lambda image, meta: {
            **meta,
            "object_height": image.shape[0],
            "object_width": image.shape[1],
            "object_bounding_box_area": image.shape[0] * image.shape[1],
        },
        image,
        meta,
    )

    # Filter based on expression
    if input_config["filter_expr"] is not None:
        logger.info(f"Filtering input by expression {input_config['filter_expr']!r}")
        FilterEval(input_config["filter_expr"], meta)

    # Detect duplicates
    build_duplicate_detection(input_config["detect_duplicates"], image, meta)

    if input_config["save_meta"]:
        # Write input metadata
        EcotaxaWriter(input_meta_archive_fn, [], meta)

    if telemetry is not None:
        # Merge telemetry
        meta = Call(merge_telemetry, meta, telemetry)

    return image, meta, target_archive_fn

def build_duplicate_detection(detect_duplicates_config: Optional[Mapping], image, meta):
    if detect_duplicates_config is None:
        return
    
    # Detect duplicates
    dupset_id = DetectDuplicatesSimple(
        meta["object_frame_id"],
        meta["object_id"],
        score_fn=score_fn_simple,
        score_arg=meta,
        min_similarity=detect_duplicates_config["min_similarity"],
        max_age=detect_duplicates_config["max_age"],
        verbose=False,
    )

    # Only keep unique objects
    Filter(dupset_id == meta["object_id"])
        

def build_pipeline(pipeline_config: Mapping):
    """
    Args:
        input_path (str): Path to LOKI data (directory containing "Pictures" and "Telemetrie").

    Example:
        python pipeline.py /media/mschroeder/LOKI-Daten/LOKI/PS101/0059_PS101-59/
    """

    with Pipeline() as p:
        image, meta, target_archive_fn = build_input(pipeline_config["input"], pipeline_config["output"])

        Progress()

        image, meta, mask = build_segmentation(pipeline_config["segmentation"], image, meta)

        StreamBuffer(8)

        build_duplicate_detection(pipeline_config["output"]["detect_duplicates"], image, meta)

        output_config = pipeline_config["output"]
        if output_config["scalebar"]:
            image = DrawScalebar(
                image,
                length_unit=1,
                px_per_unit=103,
                unit="mm",
                fg_color=255,
                bg_color=0,
            )


        # DEBUG: Slice
        if output_config["slice"] is not None:
            logger.warning(f"Only processing the first {output_config['slice']} output objects.")
            Slice(output_config["slice"])

        # # # Image enhancement: Stretch contrast
        # # image = Call(stretch_contrast, image, 0.01, 0.99)

        target_image_fn = Call(
            output_config["image_fn"].format_map, meta
        )
        output_images = [(target_image_fn, image)]
        if output_config["store_mask"]:
            target_mask_fn = Call(filename_suffix, target_image_fn, "_mask")
            output_images.append((target_mask_fn, mask))

        EcotaxaWriter(target_archive_fn, output_images, meta, store_types=output_config["type_header"])

    p.run()

def filename_suffix(fn: str, suffix: str):
    stem, ext = os.path.splitext(fn)
    return stem + suffix + ext

if __name__ == "__main__":
    import sys

    from schema import PipelineSchema

    if len(sys.argv) != 2:
        print("You need to supply a task file", file=sys.stderr)

    task_fn = sys.argv[1]

    sys.path.insert(0, os.path.realpath(os.curdir))

    os.chdir(os.path.dirname(task_fn))

    # Setup logging
    log_fn = os.path.abspath(
        f'loki_pipeline-{datetime.datetime.now().isoformat(timespec="seconds")}.log'
    )
    print(f"Logging to {log_fn}.")
    file_handler = logging.FileHandler(log_fn)
    file_handler.setLevel(logging.INFO)
    stream_handler = logging.StreamHandler()
    file_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.setLevel(logging.INFO)

    # Also capture py.warnings
    warnings_logger = logging.getLogger("py.warnings")
    warnings_logger.addHandler(file_handler)
    warnings_logger.addHandler(stream_handler)
    logger.setLevel(logging.INFO)

    with open(task_fn) as f:
        config = PipelineSchema().load(yaml.safe_load(f))
        p = build_pipeline(config)  # type: ignore

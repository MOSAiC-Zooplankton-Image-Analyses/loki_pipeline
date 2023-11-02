from typing import Mapping
from marshmallow import Schema, fields, pre_load


class DefaultSchema(Schema):
    __default_field__: str

    @pre_load
    def _convert_default(self, value, **_):
        if isinstance(value, Mapping):
            return value
        return {self.__default_field__: value}


class SegmentationPostprocessingSchema(Schema):
    closing_radius = fields.Int(load_default=0)
    opening_radius = fields.Int(load_default=0)
    merge_labels = fields.Int(load_default=0)
    min_area = fields.Int(load_default=0)
    n_threads = fields.Int(load_default=0)
    clear_border = fields.Bool(load_default=False)


class ThresholdSegmentation(DefaultSchema):
    __default_field__ = "threshold"
    threshold = fields.Number()


class StoredSegmentation(DefaultSchema):
    __default_field__ = "pickle_fn"
    pickle_fn = fields.Str()
    full_frame_archive_fn = fields.Str(load_default=None)
    skip_single = fields.Bool(load_default=False)


class StitchSchema(DefaultSchema):
    __default_field__ = "active"
    active = fields.Bool(load_default=False)
    skip_single = fields.Bool(load_default=False)


class PytorchSegmentation(DefaultSchema):
    __default_field__ = "model_fn"

    # Stitching
    stitch = fields.Nested(StitchSchema, required=False)

    model_fn = fields.Str(required=False)
    jit_model_fn = fields.Str(required=False)
    device = fields.Str(load_default="cpu")
    n_threads = fields.Int(load_default=0)
    batch_size = fields.Int(load_default=0)
    autocast = fields.Bool(load_default=False)
    dtype = fields.Str(load_default="float32")

    # Post-processing
    postprocess = fields.Nested(SegmentationPostprocessingSchema, required=False)

    # Settings for ExtractROI
    min_intensity = fields.Int(load_default=None)
    apply_mask = fields.Bool(load_default=False)
    background_color = fields.Raw(load_default=0)

    full_frame_archive_fn = fields.Str(load_default=None)


class SegmentationSchema(Schema):
    # Segmentation
    threshold = fields.Nested(ThresholdSegmentation, required=False)
    stored = fields.Nested(StoredSegmentation, required=False)
    pytorch = fields.Nested(PytorchSegmentation, required=False)

    # Filtering
    filter_expr = fields.Str(load_default=None)



class GlobInputSchema(DefaultSchema):
    __default_field__ = "pattern"
    pattern = fields.Str()

class DetectDuplicatesSchema(Schema):
    min_similarity = fields.Float(load_default=0.98)
    max_age = fields.Int(load_default=1)

class LokiInputSchema(Schema):
    # Finding input
    glob = fields.Nested(GlobInputSchema, required=False)

    # Filtering
    filter_expr = fields.Str(load_default=None)

    # Process only this many objects
    slice = fields.Int(load_default=None)

    meta = fields.Dict(required=False)
    filter_object_frame_id = fields.Str(load_default=None)
    ignore_patterns = fields.List(fields.Str, required=False)
    merge_telemetry = fields.Bool(load_default=True)
    save_meta = fields.Bool(load_default=False)

    # Detect duplicates
    detect_duplicates = fields.Nested(DetectDuplicatesSchema, load_default=None)


class InputSchema(Schema):
    loki = fields.Nested(LokiInputSchema)


class EcoTaxaOutputSchema(Schema):
    path = fields.Str()
    image_fn = fields.Str(required=False, load_default="{object_id}.jpg")
    scalebar = fields.Bool(load_default=False)
    store_mask = fields.Bool(load_default=False)
    type_header = fields.Bool(load_default=True)
    
    # Process only this many objects
    slice = fields.Int(load_default=None)

    # Detect duplicates
    detect_duplicates = fields.Nested(DetectDuplicatesSchema, load_default=None)


class OutputSchema(Schema):
    ecotaxa = fields.Nested(EcoTaxaOutputSchema)



class PipelineSchema(Schema):
    input = fields.Nested(LokiInputSchema, required=True)
    segmentation = fields.Nested(SegmentationSchema)
    output = fields.Nested(EcoTaxaOutputSchema, required=True)


if __name__ == "__main__":
    import sys
    import yaml

    with open(sys.argv[1]) as f:
        config = PipelineSchema()
        x = config.load(yaml.safe_load(f))

        print(x)

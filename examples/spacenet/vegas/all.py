import re
import random
import os
from abc import abstractmethod

import rastervision as rv
from rastervision.utils.files import list_paths
from examples.utils import str_to_bool

BUILDINGS = 'buildings'
ROADS = 'roads'


class SpacenetConfig(object):
    def __init__(self, raw_uri):
        self.raw_uri = raw_uri

    @staticmethod
    def create(raw_uri, target):
        if target.lower() == BUILDINGS:
            return VegasBuildings(raw_uri)
        elif target.lower() == ROADS:
            return VegasRoads(raw_uri)
        else:
            raise ValueError('{} is not a valid target.'.format(target))

    def get_raster_source_uri(self, id):
        return os.path.join(
            self.raw_uri, self.base_dir, self.raster_dir,
            '{}{}.tif'.format(self.raster_fn_prefix, id))

    def get_geojson_uri(self, id):
        return os.path.join(
            self.raw_uri, self.base_dir, self.label_dir,
            '{}{}.geojson'.format(self.label_fn_prefix, id))

    def get_scene_ids(self):
        label_dir = os.path.join(self.raw_uri, self.base_dir, self.label_dir)
        label_paths = list_paths(label_dir, ext='.geojson')
        label_re = re.compile(r'.*{}(\d+)\.geojson'.format(self.label_fn_prefix))
        scene_ids = [
            label_re.match(label_path).group(1)
            for label_path in label_paths]
        return scene_ids

    @abstractmethod
    def get_class_map(self):
        pass

    @abstractmethod
    def get_class_id_to_filter(self):
        pass


class VegasRoads(SpacenetConfig):
    def __init__(self, raw_uri):
        self.base_dir = 'spacenet/SN3_roads/train/AOI_2_Vegas/'
        self.raster_dir = 'PS-RGB/'
        self.label_dir = 'geojson_roads/'
        self.raster_fn_prefix = 'SN3_roads_train_AOI_2_Vegas_PS-RGB_img'
        self.label_fn_prefix = 'SN3_roads_train_AOI_2_Vegas_geojson_roads_img'
        super().__init__(raw_uri)

    def get_class_map(self):
        # First class should be background when using GeoJSONRasterSource
        return {
            'Road': (1, 'orange'),
            'Background': (2, 'black')
        }

    def get_class_id_to_filter(self):
        return {1: ['has', 'highway']}


class VegasBuildings(SpacenetConfig):
    def __init__(self, raw_uri):
        self.base_dir = 'spacenet/SN2_buildings/train/AOI_2_Vegas'
        self.raster_dir = 'PS-RGB'
        self.label_dir = 'geojson_buildings'
        self.raster_fn_prefix = 'SN2_buildings_train_AOI_2_Vegas_PS-RGB_img'
        self.label_fn_prefix = 'SN2_buildings_train_AOI_2_Vegas_geojson_buildings_img'
        super().__init__(raw_uri)

    def get_class_map(self):
        # First class should be background when using GeoJSONRasterSource
        return {
            'Building': (1, 'orange'),
            'Background': (2, 'black')
        }

    def get_class_id_to_filter(self):
        return {1: ['has', 'building']}


def build_scene(task, spacenet_config, id, channel_order=None, vector_tile_options=None):
    # Need to use stats_transformer because imagery is uint16.
    raster_source = rv.RasterSourceConfig.builder(rv.RASTERIO_SOURCE) \
                      .with_uri(spacenet_config.get_raster_source_uri(id)) \
                      .with_channel_order(channel_order) \
                      .with_stats_transformer() \
                      .build()
    label_store = None

    # Set a line buffer to convert line strings to polygons.
    if vector_tile_options is None:
        label_uri = spacenet_config.get_geojson_uri(id)
        vector_source = rv.VectorSourceConfig.builder(rv.GEOJSON_SOURCE) \
            .with_uri(label_uri) \
            .with_buffers(line_bufs={1: 15}) \
            .build()
    else:
        options = vector_tile_options
        class_id_to_filter = spacenet_config.get_class_id_to_filter()
        vector_source = rv.VectorSourceConfig.builder(rv.VECTOR_TILE_SOURCE) \
            .with_class_inference(class_id_to_filter=class_id_to_filter,
                                  default_class_id=None) \
            .with_uri(options.uri) \
            .with_zoom(options.zoom) \
            .with_id_field(options.id_field) \
            .with_buffers(line_bufs={1: 15}) \
            .build()

    if task.task_type == rv.SEMANTIC_SEGMENTATION:
        background_class_id = 2
        label_raster_source = rv.RasterSourceConfig.builder(rv.RASTERIZED_SOURCE) \
            .with_vector_source(vector_source) \
            .with_rasterizer_options(background_class_id) \
            .build()

        label_source = rv.LabelSourceConfig.builder(rv.SEMANTIC_SEGMENTATION) \
            .with_raster_source(label_raster_source) \
            .build()

        # Generate polygon output for segmented buildings.
        if isinstance(spacenet_config, VegasBuildings):
            vector_output = {'mode': 'polygons', 'class_id': 1, 'denoise': 3}
            label_store = rv.LabelStoreConfig.builder(rv.SEMANTIC_SEGMENTATION_RASTER) \
                                             .with_vector_output([vector_output]) \
                                             .build()

    elif task.task_type == rv.CHIP_CLASSIFICATION:
        label_source = rv.LabelSourceConfig.builder(rv.CHIP_CLASSIFICATION) \
                                           .with_vector_source(vector_source) \
                                           .with_ioa_thresh(0.01) \
                                           .with_use_intersection_over_cell(True) \
                                           .with_pick_min_class_id(True) \
                                           .with_background_class_id(2) \
                                           .with_infer_cells(True) \
                                           .build()
    elif task.task_type == rv.OBJECT_DETECTION:
        label_source = rv.LabelSourceConfig.builder(rv.OBJECT_DETECTION) \
                                           .with_vector_source(vector_source) \
                                           .build()

    scene = rv.SceneConfig.builder() \
                          .with_task(task) \
                          .with_id(id) \
                          .with_raster_source(raster_source) \
                          .with_label_source(label_source) \
                          .with_label_store(label_store) \
                          .build()

    return scene


def build_dataset(task, spacenet_config, test, vector_tile_options=None):
    scene_ids = spacenet_config.get_scene_ids()
    if len(scene_ids) == 0:
        raise ValueError('No scenes found. Something is configured incorrectly.')
    random.seed(5678)
    scene_ids = sorted(scene_ids)
    random.shuffle(scene_ids)
    # Workaround to handle scene 1000 missing on S3.
    if '1000' in scene_ids:
        scene_ids.remove('1000')
    split_ratio = 0.8
    num_train_ids = round(len(scene_ids) * split_ratio)
    train_ids = scene_ids[0:num_train_ids]
    val_ids = scene_ids[num_train_ids:]

    num_train_scenes = len(train_ids)
    num_val_scenes = len(val_ids)
    if test:
        num_train_scenes = 16
        num_val_scenes = 4
    train_ids = train_ids[0:num_train_scenes]
    val_ids = val_ids[0:num_val_scenes]
    channel_order = [0, 1, 2]

    train_scenes = [build_scene(task, spacenet_config, id, channel_order,
                                vector_tile_options=vector_tile_options)
                    for id in train_ids]
    val_scenes = [build_scene(task, spacenet_config, id, channel_order,
                              vector_tile_options=vector_tile_options)
                  for id in val_ids]
    dataset = rv.DatasetConfig.builder() \
                              .with_train_scenes(train_scenes) \
                              .with_validation_scenes(val_scenes) \
                              .build()
    return dataset


def build_task(task_type, class_map):
    if task_type == rv.SEMANTIC_SEGMENTATION:
        task = rv.TaskConfig.builder(rv.SEMANTIC_SEGMENTATION) \
                            .with_chip_size(300) \
                            .with_classes(class_map) \
                            .with_chip_options(
                                chips_per_scene=9,
                                debug_chip_probability=0.25,
                                negative_survival_probability=1.0,
                                target_classes=[1],
                                target_count_threshold=1000) \
                            .build()
    elif task_type == rv.CHIP_CLASSIFICATION:
        task = rv.TaskConfig.builder(rv.CHIP_CLASSIFICATION) \
                    .with_chip_size(200) \
                    .with_classes(class_map) \
                    .build()
    elif task_type == rv.OBJECT_DETECTION:
        task = rv.TaskConfig.builder(rv.OBJECT_DETECTION) \
                            .with_chip_size(300) \
                            .with_classes(class_map) \
                            .with_chip_options(neg_ratio=1.0,
                                               ioa_thresh=0.8) \
                            .with_predict_options(merge_thresh=0.1,
                                                  score_thresh=0.5) \
                            .build()

    return task


def build_backend(task, test):
    debug = False
    if test:
        debug = True

    if task.task_type == rv.SEMANTIC_SEGMENTATION:
        batch_size = 8
        num_epochs = 2
        if test:
            batch_size = 2
            num_epochs = 1

        backend = rv.BackendConfig.builder(rv.PYTORCH_SEMANTIC_SEGMENTATION) \
            .with_task(task) \
            .with_train_options(
                lr=1e-4,
                batch_size=batch_size,
                num_epochs=num_epochs,
                model_arch='resnet50',
                debug=debug) \
            .build()
    elif task.task_type == rv.CHIP_CLASSIFICATION:
        num_epochs = 2
        batch_size = 32
        if test:
            num_epochs = 1
            batch_size = 2

        backend = rv.BackendConfig.builder(rv.PYTORCH_CHIP_CLASSIFICATION) \
            .with_task(task) \
            .with_train_options(
                batch_size=batch_size,
                num_epochs=num_epochs,
                model_arch='resnet18',
                debug=debug) \
            .build()
    elif task.task_type == rv.OBJECT_DETECTION:
        batch_size = 16
        num_epochs = 2
        if test:
            batch_size = 1
            num_epochs = 2

        backend = rv.BackendConfig.builder(rv.PYTORCH_OBJECT_DETECTION) \
            .with_task(task) \
            .with_train_options(
                lr=1e-4,
                one_cycle=True,
                batch_size=batch_size,
                num_epochs=num_epochs,
                model_arch='resnet18',
                debug=debug) \
            .build()

    return backend


def str_to_bool(x):
    if type(x) == str:
        if x.lower() == 'true':
            return True
        elif x.lower() == 'false':
            return False
        else:
            raise ValueError('{} is expected to be true or false'.format(x))
    return x


def validate_options(task_type, target, vector_tile_options=None):
    if task_type not in [rv.SEMANTIC_SEGMENTATION, rv.CHIP_CLASSIFICATION,
                         rv.OBJECT_DETECTION]:
        raise ValueError('{} is not a valid task_type'.format(task_type))

    if target not in [ROADS, BUILDINGS]:
        raise ValueError('{} is not a valid target'.format(target))

    if target == ROADS:
        if task_type in [rv.OBJECT_DETECTION]:
            raise ValueError('{} is not valid task_type for target="roads"'.format(
                task_type))

    if vector_tile_options is not None:
        if len(vector_tile_options.split(',')) != 3:
            raise ValueError(
                'vector_tile_options needs to have 3 comma-delimited values')


class VectorTileOptions():
    def __init__(self, uri, zoom, id_field):
        self.uri = uri
        self.zoom = int(zoom)
        self.id_field = id_field

    @staticmethod
    def build(config_str):
        if config_str is None:
            return None
        else:
            uri, zoom, id_field = config_str.split(',')
            return VectorTileOptions(uri, zoom, id_field)


class SpacenetVegas(rv.ExperimentSet):
    def exp_main(self, raw_uri, root_uri, test=False,
                 target=BUILDINGS, task_type=rv.SEMANTIC_SEGMENTATION,
                 vector_tile_options=None):
        """Run an experiment on the Spacenet Vegas road or building dataset.

        This is an example of how to do all three tasks on the same dataset.

        Args:
            raw_uri: (str) directory of raw data (the root of the Spacenet dataset)
            root_uri: (str) root directory for experiment output
            test: (bool) if True, run a very small experiment as a test and generate
                debug output
            target: (str) 'buildings' or 'roads'
            task_type: (str) 'semantic_segmentation', 'object_detection', or
                'chip_classification'
            vector_tile_options: (str or None) space delimited list of uri, zoom, and
                id_field. See VectorTileVectorSourceConfigBuilder.with_uri, .with_zoom
                and .with_id_field methods for more details.
        """
        test = str_to_bool(test)
        exp_id = '{}-{}'.format(target, task_type.lower())
        task_type = task_type.upper()
        spacenet_config = SpacenetConfig.create(raw_uri, target)
        validate_options(task_type, target, vector_tile_options)
        vector_tile_options = VectorTileOptions.build(vector_tile_options)

        task = build_task(task_type, spacenet_config.get_class_map())
        backend = build_backend(task, test)
        analyzer = rv.AnalyzerConfig.builder(rv.STATS_ANALYZER) \
                                    .build()
        dataset = build_dataset(task, spacenet_config, test,
                                vector_tile_options=vector_tile_options)

        # Need to use stats_analyzer because imagery is uint16.
        experiment = rv.ExperimentConfig.builder() \
                                        .with_id(exp_id) \
                                        .with_task(task) \
                                        .with_backend(backend) \
                                        .with_analyzer(analyzer) \
                                        .with_dataset(dataset) \
                                        .with_root_uri(root_uri) \
                                        .build()

        return experiment


if __name__ == '__main__':
    rv.main()

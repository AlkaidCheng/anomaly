from typing import Dict, List, Optional, Union
import numpy as np
import awkward as ak

from quickstats import AbstractObject
from quickstats.utils.common_utils import combine_dict

from aliad.interface.awkward.utils import get_record_outer_shapes

class PointCloudDataset(AbstractObject):
    """
    This loader will load data in memory. Improvements to be made to load lazily.
    """
    DEFAULT_FEATURE_DICT = {
        "part_coords"   : ["part_delta_eta", "part_delta_phi"],
        "part_features" : ["part_pt", "part_delta_eta", "part_delta_phi", "part_delta_R"],
        "jet_features"  : ["jet_pt", "jet_eta", "jet_phi", "jet_m", "N", "tau12", "tau23"]
    }
    
    def __init__(self, filenames:Union[Dict, str],
                 feature_dict:Dict,
                 class_labels:Dict,
                 samples:Optional[List[str]]=None,
                 sample_sizes:Optional[Dict]=None,
                 num_jets:int=2, pad_size:int=300,
                 shuffle:bool=False, seed:int=2023,
                 normalize_weight:bool=True,
                 verbosity:str="INFO"):
        super().__init__(verbosity=verbosity)
        self.set_class_labels(class_labels)
        self.feature_dict = combine_dict(feature_dict)
        self.sample_sizes = combine_dict(sample_sizes)
        self.num_jets = num_jets
        self.pad_size  = pad_size
        self.shuffle = shuffle
        self.seed = seed
        self._features = {}
        self._labels   = None
        self._weights  = None
        self.load(filenames, samples=samples)

    def __len__(self):
        return self._labels.shape[0]

    def set_class_labels(self, class_labels:Dict):
        class_labels = combine_dict(class_labels)
        label_map = {}
        for class_value, labels in class_labels.items():
            for label in labels:
                if label in label_map:
                    raise ValueError(f'label "{label}" defined in multiple classes')
                label_map[label] = int(class_value)
        self.class_labels = class_labels
        self.label_map = label_map

    @staticmethod
    def is_ragged(array):
        return isinstance(array.type.content, ak.types.ListType)

    @staticmethod
    def get_padded_array(array, pad_size:int, clip:bool=True, pad_val:float=0,
                         sample_size:Optional[int]=None):
        if sample_size is None:
            return ak.to_numpy(ak.fill_none(ak.pad_none(array, pad_size, clip=clip), pad_val))
        return ak.to_numpy(ak.fill_none(ak.pad_none(array[:sample_size], pad_size, clip=clip), pad_val))

    @staticmethod
    def get_array(array, sample_size:Optional[int]=None):
        if sample_size is None:
            return ak.to_numpy(array)
        return ak.to_numpy(array[:sample_size])
                  
    @staticmethod
    def get_mask_array(array, pad_size:int, clip:bool=True, sample_size:Optional[int]=None):
        if sample_size is None:
            return ak.to_numpy(ak.pad_none(array, pad_size, clip=clip)).mask
        return ak.to_numpy(ak.pad_none(array[:sample_size], pad_size, clip=clip)).mask

    def _load_sample_data(self, filenames:Union[Dict, str], sample:str):
        def _load_file(filename:str):
            self.stdout.info(f'Loading dataset from "{filename}"')
            return ak.from_parquet(filename)
        if isinstance(filenames, str):
            if (self.cache_arrays is not None):
                if sample not in ak.fields(self.cache_arrays):
                    raise RuntimeError(f'dataset does not contain the sample "{sample}"')
                return None
            else:
                self.cache_arrays = _load_file(filenames)
            self.cache_sample_arrays = self.cache_arrays[sample]
        elif isinstance(filenames, dict):
            if sample not in filenames:
                raise ValueError(f'no input file specified for the sample "{sample}"')
            self.cache_arrays = _load_file(filenames[sample])
            self.cache_sample_arrays = self.cache_arrays
        else:
            raise ValueError('filenames must be a string or a dictionary')

    def clear_cache(self):
        self.cache_arrays = None
        self.cache_sample_arrays = None
        
    def load(self, filenames:Union[Dict, str], samples:Optional[List[str]]=None):
        self.clear_cache()
        features = {}
        labels = []
        masks  = []
        np.random.seed(self.seed)
        if samples is None:
            samples = list(self.label_map)
        class_sizes = {}
        for sample in samples:
            if sample not in self.label_map:
                raise RuntimeError(f'no class value defined for the sample "{sample}"')
            self._load_sample_data(filenames, sample)
            sample_arrays = self.cache_sample_arrays
            class_val = self.label_map.get(sample)
            self.stdout.info(f'Preparing data for the sample "{sample}" (class = {class_val})')
            sample_size = len(sample_arrays)
            sample_size_ = self.sample_sizes.get(sample, None)
            if (sample_size_ is not None):
                if sample_size > sample_size_:
                    raise RuntimeError('can not request more events than is available for the '
                                       f'sample "{sample}" (requested = {sample_size_}, available = {sample_size}')
                sample_size = sample_size_
            self.stdout.info(f'Size of sample data: {sample_size}')
            labels.append(np.full(sample_size, class_val))
            if class_val not in class_sizes:
                class_sizes[class_val] = 0
            class_sizes[class_val] += sample_size
            mask_arrays = None
            for feature_type in self.feature_dict:
                self.stdout.info(f'Working on feature type "{feature_type}"')
                if feature_type not in features:
                    features[feature_type] = []
                columns = self.feature_dict[feature_type]
                jet_feature_arrays = []
                jet_mask_arrays = []
                for i in range(1, self.num_jets + 1):
                    self.stdout.info(f'Jet index: {i}')
                    jet_key = f'j{i}'
                    feature_arrays = []
                    mask_array = None
                    for column in columns:
                        self.stdout.info(f'Loading data for the feature "{column}"')
                        arrays = sample_arrays[jet_key][column]
                        if self.is_ragged(arrays):
                            # only get the mask once per jet
                            if (mask_array is None):
                                mask_array = self.get_mask_array(arrays, self.pad_size,
                                                                 sample_size=sample_size)
                            arrays = self.get_padded_array(arrays, self.pad_size,
                                                           sample_size=sample_size)
                        else:
                            arrays = self.get_array(arrays, sample_size=sample_size)
                        feature_arrays.append(arrays)
                    # shape = (nevent, nparticles, nfeatuers)
                    feature_arrays = np.stack(feature_arrays, -1)
                    if mask_array is not None:
                        jet_mask_arrays.append(mask_array)
                    jet_feature_arrays.append(feature_arrays)
                # shape = (nevent, njet, nparticles, nfeatuers)
                jet_feature_arrays = np.stack(jet_feature_arrays, axis=1)
                features[feature_type].append(jet_feature_arrays)
                if jet_mask_arrays:
                    mask_arrays = np.stack(jet_mask_arrays, axis=1)
            if mask_arrays is not None:
                masks.append(mask_arrays)
        self.stdout.info(f'Combining data from all samples')
        sizes = []
        for feature_type in features:
            features[feature_type] = np.concatenate(features[feature_type])
            sizes.append(features[feature_type].shape[0])
        labels = np.concatenate(labels)
        if len(set(sizes)) != 1:
            raise RuntimeError("inconsistent sample size in different feature types")
        if sizes[0] != labels.shape[0]:
            raise RuntimeError("inconsistent sample size between features and labels")
        if masks:
            masks  = np.concatenate(masks)
            assert masks.shape[0] == sizes[0]
        else:
            masks  = None
        if self.shuffle:
            self.stdout.info(f"Shuffling events (size = {sizes[0]})")
            index = np.arange(sizes[0])
            np.random.shuffle(index)
            for feature_type in features:
                features[feature_type] = features[feature_type][index]
            labels = labels[index]
            weights = weights[index]
            if masks is not None:
                masks = masks[index]
        if masks is not None:
            features['part_masks'] = masks
        weights = np.ones(labels.shape)
        for class_val, size in class_sizes.items():
            mask = np.where(labels == class_val)
            weights[mask] /= size
        labels = np.expand_dims(labels, axis=-1)
        weights = np.expand_dims(weights, axis=-1)
        self._features = features
        self._labels = labels
        self._weights = weights
        self.clear_cache()

    @property
    def X(self):
        return self._features
    
    @property
    def y(self):
        return self._labels

    @property
    def weight(self):
        return self._weights

    @property
    def masks(self):
        return self.features.get('part_masks', None)

    def clear(self):
        self._features = None
        self._labels   = None
        self._weights  = None
        self._masks    = None
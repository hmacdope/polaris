import json
from hashlib import md5
from typing import Dict, List, Optional, Tuple, Union

import fsspec
import numpy as np
import pandas as pd
import zarr
from loguru import logger
from pydantic import (
    Field,
    PrivateAttr,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from polaris._artifact import BaseArtifactModel
from polaris.dataset._adapters import Adapter
from polaris.dataset._column import ColumnAnnotation
from polaris.hub.polarisfs import PolarisFileSystem
from polaris.utils import fs
from polaris.utils.constants import DEFAULT_CACHE_DIR
from polaris.utils.dict2html import dict2html
from polaris.utils.errors import InvalidDatasetError, PolarisChecksumError
from polaris.utils.types import AccessType, HttpUrlString, HubOwner, License

# Constants
_SUPPORTED_TABLE_EXTENSIONS = ["parquet"]
_CACHE_SUBDIR = "datasets"
_INDEX_SEP = "#"


class Dataset(BaseArtifactModel):
    """Basic data-model for a Polaris dataset, implemented as a [Pydantic](https://docs.pydantic.dev/latest/) model.

    At its core, a dataset in Polaris is a tabular data structure that stores data-points in a row-wise manner.
    A Dataset can have multiple modalities or targets, can be sparse and can be part of one or multiple
     [`BenchmarkSpecification`][polaris.benchmark.BenchmarkSpecification] objects.

    Info: Pointer columns
        Whereas a `Dataset` contains all information required to construct a dataset, it is not ready yet.
        For complex data, such as images, we support storing the content in external blobs of data.
        In that case, the table contains _pointers_ to these blobs that are dynamically loaded when needed.

    Attributes:
        table: The core data-structure, storing data-points in a row-wise manner. Can be specified as either a
            path to a `.parquet` file or a `pandas.DataFrame`.
        default_adapters: The adapters that the Dataset recommends to use by default to change the format of the data
            for specific columns.
        zarr_archive: The data for any pointer column should be saved in the Zarr archive this path points to.
        md5sum: The checksum is used to verify the version of the dataset specification. If specified, it will
            raise an error if the specified checksum doesn't match the computed checksum.
        readme: Markdown text that can be used to provide a formatted description of the dataset.
            If using the Polaris Hub, it is worth noting that this field is more easily edited through the Hub UI
            as it provides a rich text editor for writing markdown.
        annotations: Each column _can be_ annotated with a [`ColumnAnnotation`][polaris.dataset.ColumnAnnotation] object.
            Importantly, this is used to annotate whether a column is a pointer column.
        source: The data source, e.g. a DOI, Github repo or URI.
        license: The dataset license
        curation_reference: A reference to the curation process, e.g. a DOI, Github repo or URI.
    For additional meta-data attributes, see the [`BaseArtifactModel`][polaris._artifact.BaseArtifactModel] class.

    Raises:
        InvalidDatasetError: If the dataset does not conform to the Pydantic data-model specification.
        PolarisChecksumError: If the specified checksum does not match the computed checksum.
    """

    # Public attributes
    # Data
    table: Union[pd.DataFrame, str]
    default_adapters: Dict[str, Adapter] = Field(default_factory=dict)
    zarr_archive: Optional[str] = None
    md5sum: Optional[str] = None

    # Additional meta-data
    readme: str = ""
    annotations: Dict[str, ColumnAnnotation] = Field(default_factory=dict)
    source: Optional[HttpUrlString] = None
    license: Optional[License] = None
    curation_reference: Optional[HttpUrlString] = None

    # Config
    cache_dir: Optional[str] = None  # Where to cache the data to if cache() is called.

    # Private attributes
    _zarr_root: Optional[zarr.Group] = PrivateAttr(None)
    _client = PrivateAttr(None)  # Optional[PolarisHubClient]
    _has_been_warned: bool = False
    _has_been_cached: bool = False

    @field_validator("table")
    def _validate_table(cls, v):
        """
        If the table is not a dataframe yet, assume it's a path and try load it.
        We also make sure that the pandas index is contiguous and starts at 0, and
        that all columns are named and unique.
        """
        # Load from path if not a dataframe
        if not isinstance(v, pd.DataFrame):
            if not fs.is_file(v) or fs.get_extension(v) not in _SUPPORTED_TABLE_EXTENSIONS:
                raise InvalidDatasetError(f"{v} is not a valid DataFrame or .parquet path.")
            v = pd.read_parquet(v)
        # Check if there are any duplicate columns
        if any(v.columns.duplicated()):
            raise InvalidDatasetError("The table contains duplicate columns")
        # Check if there are any unnamed columns
        if not all(isinstance(c, str) for c in v.columns):
            raise InvalidDatasetError("The table contains unnamed columns")
        # Make sure the index is contiguous and starts at 0
        v = v.reset_index(drop=True)
        return v

    @model_validator(mode="after")
    @classmethod
    def _validate_model(cls, m: "Dataset"):
        """If a checksum is provided, verify it matches what the checksum should be.
        If no checksum is provided, make sure it is set.
        If no cache_dir is provided, set it to the default cache dir and make sure it exists
        """

        # Verify that all annotations are for columns that exist
        if any(k not in m.table.columns for k in m.annotations):
            raise InvalidDatasetError("There are annotations for columns that do not exist")

        # Verify that all adapters are for columns that exist
        if any(k not in m.table.columns for k in m.default_adapters.keys()):
            raise InvalidDatasetError("There are default adapters for columns that do not exist")

        # Set a default for missing annotations and convert strings to Modality
        for c in m.table.columns:
            if c not in m.annotations:
                m.annotations[c] = ColumnAnnotation()
            m.annotations[c].dtype = m.table[c].dtype

        # Verify the checksum
        # NOTE (cwognum): Is it still reasonable to always verify this as the dataset size grows?
        actual = m.md5sum
        expected = cls._compute_checksum(m.table)

        if actual is None:
            m.md5sum = expected
        elif actual != expected:
            raise PolarisChecksumError(
                "The dataset md5sum does not match what was specified in the meta-data. "
                f"{actual} != {expected}"
            )

        # Set the default cache dir if none and make sure it exists
        if m.cache_dir is None:
            m.cache_dir = fs.join(DEFAULT_CACHE_DIR, _CACHE_SUBDIR, m.name, m.md5sum)
        fs.mkdir(m.cache_dir, exist_ok=True)

        return m

    @field_validator("default_adapters")
    def _validate_adapters(cls, value):
        """Serializes the adapters"""
        return {k: Adapter[v] if isinstance(v, str) else v for k, v in value.items()}

    @field_serializer("default_adapters")
    def _serialize_adapters(self, value: List[Adapter]):
        """Serializes the adapters"""
        return {k: v.name for k, v in value.items()}

    @staticmethod
    def _compute_checksum(table):
        """Computes a hash of the dataset.

        This is meant to uniquely identify the dataset and can be used to verify the version.

        1. Is not sensitive to the ordering of the columns or rows in the table.
        2. Purposefully does not include the meta-data (source, description, name, annotations).
        3. For any pointer column, it uses a hash of the path instead of the file contents.
            This is a limitation, but probably a reasonable assumption that helps practicality.
            A big downside is that as the dataset is saved elsewhere, the hash changes.
        """
        hash_fn = md5()

        # Sort the columns s.t. the checksum is not sensitive to the column-ordering
        df = table.copy(deep=True)
        df = df[sorted(df.columns.tolist())]

        # Use the sum of the row-wise hashes s.t. the hash is insensitive to the row-ordering
        table_hash = pd.util.hash_pandas_object(df, index=False).sum()
        hash_fn.update(table_hash)

        checksum = hash_fn.hexdigest()
        return checksum

    @property
    def client(self):
        """The Polaris Hub client used to interact with the Polaris Hub."""

        # Import it here to prevent circular imports
        from polaris.hub.client import PolarisHubClient

        if self._client is None:
            self._client = PolarisHubClient()
        return self._client

    @property
    def zarr_root(self):
        """Open the zarr archive in read-write mode if it is not already open."""
        if not any(anno.is_pointer for anno in self.annotations.values()):
            return None
        if self._zarr_root is None:
            options = {}
            if PolarisFileSystem.protocol in self.zarr_archive:
                options["client"] = self.client
                options["dataset_owner"] = self.owner
                options["dataset_name"] = self.name
            self._zarr_root = zarr.open(self.zarr_archive, "a", storage_options=options)
        return self._zarr_root

    @computed_field
    @property
    def n_rows(self) -> int:
        """The number of rows in the dataset."""
        return len(self.rows)

    @computed_field
    @property
    def n_columns(self) -> int:
        """The number of columns in the dataset."""
        return len(self.columns)

    @property
    def rows(self) -> list:
        """Return all row indices for the dataset"""
        return self.table.index.tolist()

    @property
    def columns(self) -> list:
        """Return all columns for the dataset"""
        return self.table.columns.tolist()

    def get_data(self, row: int, col: str, adapters: Optional[List[Adapter]] = None) -> np.ndarray:
        """Since the dataset might contain pointers to external files, data retrieval is more complicated
        than just indexing the `table` attribute. This method provides an end-point for seamlessly
        accessing the underlying data.

        Args:
            row: The row index in the `Dataset.table` attribute
            col: The column index in the `Dataset.table` attribute
            adapters: The adapters to apply to the data before returning it.
                If None, will use the default adapters specified for the dataset.

        Returns:
            A numpy array with the data at the specified indices. If the column is a pointer column,
                the content of the referenced file is loaded to memory.
        """

        adapters = adapters or self.default_adapters

        def _load(p: str, index: Union[int, slice]) -> np.ndarray:
            """Tiny helper function to reduce code repetition."""
            arr = self.zarr_root[p][index]

            if isinstance(index, slice):
                arr = tuple(arr)

            adapter = adapters.get(col)
            if adapter is not None:
                arr = adapter(arr)

            return arr

        value = self.table.loc[row, col]
        if not self.annotations[col].is_pointer:
            return value

        value, index = self._split_index_from_path(value)

        # In the case it is a pointer column, we need to load additional data into memory
        # We first check if the data has been downloaded to the cache.
        if self._has_been_cached:
            return _load(value, index)

        # If it doesn't exist, we load from the original path and warn if not local
        if not fs.is_local_path(self.zarr_archive) and not self._has_been_warned:
            logger.warning(
                f"You're loading data from a remote location. "
                f"To speed up this process, consider caching the dataset first "
                f"using {self.__class__.__name__}.cache()"
            )
            self._has_been_warned = True
        return _load(value, index)

    def upload_to_hub(
        self, access: Optional[AccessType] = "private", owner: Optional[Union[HubOwner, str]] = None
    ):
        """
        Very light, convenient wrapper around the
        [`PolarisHubClient.upload_dataset`][polaris.hub.client.PolarisHubClient.upload_dataset] method.
        """
        self.client.upload_dataset(self, access=access, owner=owner)

    @classmethod
    def from_json(cls, path: str):
        """Loads a benchmark from a JSON file.
        Overrides the method from the base class to remove the caching dir from the file to load from,
        as that should be user dependent.

        Args:
            path: Loads a benchmark specification from a JSON file.
        """
        with fsspec.open(path, "r") as f:
            data = json.load(f)
        data.pop("cache_dir", None)
        return cls.model_validate(data)

    def to_json(self, destination: str) -> str:
        """
        Save the dataset to a destination directory as a JSON file.

        Warning: Multiple files
            Perhaps unintuitive, this method creates multiple files.

            1. `/path/to/destination/dataset.json`: This file can be loaded with
                [`Dataset.from_json`][polaris.dataset.Dataset.from_json].
            2. `/path/to/destination/table.parquet`: The `Dataset.table` attribute is saved here.
            3. _(Optional)_ `/path/to/destination/data/*`: Any additional blobs of data referenced by the
                    pointer columns will be stored here.

        Args:
            destination: The _directory_ to save the associated data to.

        Returns:
            The path to the JSON file.
        """
        fs.mkdir(destination, exist_ok=True)
        table_path = fs.join(destination, "table.parquet")
        dataset_path = fs.join(destination, "dataset.json")
        zarr_archive = fs.join(destination, "data.zarr")

        # Lu: Avoid serilizing and sending None to hub app.
        serialized = self.model_dump(exclude={"cache_dir"}, exclude_none=True)
        serialized["table"] = table_path

        # Copy over Zarr data to the destination
        if self.zarr_root is not None:
            dest = zarr.open(zarr_archive, "w")
            zarr.copy_all(source=self.zarr_root, dest=dest)
            serialized["zarr_archive"] = zarr_archive

        self.table.to_parquet(table_path)
        with fsspec.open(dataset_path, "w") as f:
            json.dump(serialized, f)

        return dataset_path

    def cache(self, cache_dir: Optional[str] = None) -> str:
        """Caches the dataset by downloading all additional data for pointer columns to a local directory.

        Args:
            cache_dir: The directory to cache the data to. If not provided,
                this will fall back to the `Dataset.cache_dir` attribute

        Returns:
            The path to the cache directory.
        """

        if cache_dir is not None:
            self.cache_dir = cache_dir

        self.to_json(self.cache_dir)

        if self.zarr_archive is not None:
            self.zarr_archive = fs.join(self.cache_dir, "data.zarr")

        if not self._has_been_cached:
            self._has_been_cached = True
        return self.cache_dir

    def size(self):
        return self.rows, self.n_columns

    def _split_index_from_path(self, path: str) -> Tuple[str, Optional[int]]:
        """
        Paths can have an additional index appended to them.
        This extracts that index from the path.
        """
        index = None
        if _INDEX_SEP in path:
            path, index = path.split(_INDEX_SEP)
            index = index.split(":")

            if len(index) == 1:
                index = int(index[0])
            elif len(index) == 2:
                index = slice(int(index[0]), int(index[1]))
            else:
                raise ValueError(f"Invalid index format: {index}")
        return path, index

    def __getitem__(self, item):
        """Allows for indexing the dataset directly"""
        ret = self.table.loc[item]
        if isinstance(ret, pd.Series):
            # Load the data from the pointer columns

            if ret.name in self.table.columns:
                # Returning a column, the indices are rows
                if self.annotations[ret.name].is_pointer:
                    ret = np.array([self.get_data(k, ret.name) for k in ret.index])

            elif len(ret) == self.n_rows:
                # Returning a row, the indices are columns
                ret = {
                    k: self.get_data(k, ret.name) if self.annotations[ret.name].is_pointer else ret[k]
                    for k in ret.index
                }

        # Returning a dataframe
        if isinstance(ret, pd.DataFrame):
            for c in ret.columns:
                if self.annotations[c].is_pointer:
                    ret[c] = [self.get_data(item, c) for item in ret.index]
            return ret

        return ret

    def _repr_dict_(self) -> dict:
        """Utility function for pretty-printing to the command line and jupyter notebooks"""
        repr_dict = self.model_dump()
        repr_dict.pop("table")
        return repr_dict

    def _repr_html_(self):
        """For pretty-printing in Jupyter Notebooks"""
        return dict2html(self._repr_dict_())

    def __len__(self):
        return self.n_rows

    def __repr__(self):
        return json.dumps(self._repr_dict_(), indent=2)

    def __str__(self):
        return self.__repr__()

    def __eq__(self, other):
        """Whether two datasets are equal is solely determined by the checksum"""
        if not isinstance(other, Dataset):
            return False
        return self.md5sum == other.md5sum

    def __del__(self):
        """Close the connection of the client"""
        if self._client is not None:
            self._client.close()

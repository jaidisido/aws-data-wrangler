"""Amazon S3 Read PARQUET Module (PRIVATE)."""

import concurrent.futures
import datetime
import itertools
import logging
import pprint
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

import boto3  # type: ignore
import pandas as pd  # type: ignore
import pyarrow as pa  # type: ignore
import pyarrow.lib  # type: ignore
import pyarrow.parquet  # type: ignore
import s3fs  # type: ignore

from awswrangler import _data_types, _utils, exceptions
from awswrangler.s3._list import _path2list
from awswrangler.s3._read import (
    _apply_partition_filter,
    _apply_partitions,
    _extract_partitions_dtypes_from_table_details,
    _extract_partitions_metadata_from_paths,
    _get_path_ignore_suffix,
    _get_path_root,
    _union,
)
from awswrangler.s3._read_concurrent import _read_concurrent

_logger: logging.Logger = logging.getLogger(__name__)


def _read_parquet_metadata_file(
    path: str, boto3_session: boto3.Session, s3_additional_kwargs: Optional[Dict[str, str]],
) -> Dict[str, str]:
    fs: s3fs.S3FileSystem = _utils.get_fs(
        s3fs_block_size=4_194_304, session=boto3_session, s3_additional_kwargs=s3_additional_kwargs  # 4 MB (4 * 2**20)
    )
    with _utils.open_file(fs=fs, path=path, mode="rb") as f:
        pq_file: pyarrow.parquet.ParquetFile = pyarrow.parquet.ParquetFile(source=f)
        return _data_types.athena_types_from_pyarrow_schema(schema=pq_file.schema.to_arrow_schema(), partitions=None)[0]


def _read_schemas_from_files(
    paths: List[str],
    sampling: float,
    use_threads: bool,
    boto3_session: boto3.Session,
    s3_additional_kwargs: Optional[Dict[str, str]],
) -> Tuple[Dict[str, str], ...]:
    paths = _utils.list_sampling(lst=paths, sampling=sampling)
    schemas: Tuple[Dict[str, str], ...] = tuple()
    n_paths: int = len(paths)
    if use_threads is False or n_paths == 1:
        schemas = tuple(
            _read_parquet_metadata_file(path=p, boto3_session=boto3_session, s3_additional_kwargs=s3_additional_kwargs,)
            for p in paths
        )
    elif n_paths > 1:
        cpus: int = _utils.ensure_cpu_count(use_threads=use_threads)
        with concurrent.futures.ThreadPoolExecutor(max_workers=cpus) as executor:
            schemas = tuple(
                executor.map(
                    _read_parquet_metadata_file,
                    paths,
                    itertools.repeat(_utils.boto3_to_primitives(boto3_session=boto3_session)),  # Boto3.Session
                    itertools.repeat(s3_additional_kwargs),
                )
            )
    _logger.debug("schemas: %s", schemas)
    return schemas


def _validate_schemas(schemas: Tuple[Dict[str, str], ...]) -> None:
    if len(schemas) < 2:
        return None
    first: Dict[str, str] = schemas[0]
    for schema in schemas[1:]:
        if first != schema:
            raise exceptions.InvalidSchemaConvergence(
                f"Was detect at least 2 different schemas:\n    1 - {first}\n    2 - {schema}."
            )
    return None


def _validate_schemas_from_files(
    paths: List[str],
    sampling: float,
    use_threads: bool,
    boto3_session: boto3.Session,
    s3_additional_kwargs: Optional[Dict[str, str]],
) -> None:
    schemas: Tuple[Dict[str, str], ...] = _read_schemas_from_files(
        paths=paths,
        sampling=sampling,
        use_threads=use_threads,
        boto3_session=boto3_session,
        s3_additional_kwargs=s3_additional_kwargs,
    )
    _validate_schemas(schemas=schemas)


def _merge_schemas(schemas: Tuple[Dict[str, str], ...]) -> Dict[str, str]:
    columns_types: Dict[str, str] = {}
    for schema in schemas:
        for column, dtype in schema.items():
            if (column in columns_types) and (columns_types[column] != dtype):
                raise exceptions.InvalidSchemaConvergence(
                    f"Was detect at least 2 different types in column {column} ({columns_types[column]} and {dtype})."
                )
            columns_types[column] = dtype
    return columns_types


def _read_parquet_metadata(
    path: Union[str, List[str]],
    path_suffix: Optional[str],
    path_ignore_suffix: Optional[str],
    dtype: Optional[Dict[str, str]],
    sampling: float,
    dataset: bool,
    use_threads: bool,
    boto3_session: boto3.Session,
    s3_additional_kwargs: Optional[Dict[str, str]],
) -> Tuple[Dict[str, str], Optional[Dict[str, str]], Optional[Dict[str, List[str]]]]:
    """Handle wr.s3.read_parquet_metadata internally."""
    path_root: Optional[str] = _get_path_root(path=path, dataset=dataset)
    paths: List[str] = _path2list(
        path=path,
        boto3_session=boto3_session,
        suffix=path_suffix,
        ignore_suffix=_get_path_ignore_suffix(path_ignore_suffix=path_ignore_suffix),
    )

    # Files
    schemas: Tuple[Dict[str, str], ...] = _read_schemas_from_files(
        paths=paths,
        sampling=sampling,
        use_threads=use_threads,
        boto3_session=boto3_session,
        s3_additional_kwargs=s3_additional_kwargs,
    )
    columns_types: Dict[str, str] = _merge_schemas(schemas=schemas)

    # Partitions
    partitions_types: Optional[Dict[str, str]] = None
    partitions_values: Optional[Dict[str, List[str]]] = None
    if (dataset is True) and (path_root is not None):
        partitions_types, partitions_values = _extract_partitions_metadata_from_paths(path=path_root, paths=paths)

    # Casting
    if dtype:
        for k, v in dtype.items():
            if columns_types and k in columns_types:
                columns_types[k] = v
            if partitions_types and k in partitions_types:
                partitions_types[k] = v

    return columns_types, partitions_types, partitions_values


def _arrowtable2df(
    table: pa.Table,
    categories: Optional[List[str]],
    safe: bool,
    use_threads: bool,
    dataset: bool,
    path: str,
    path_root: Optional[str],
) -> pd.DataFrame:
    return _apply_partitions(
        df=table.to_pandas(
            use_threads=use_threads,
            split_blocks=True,
            self_destruct=True,
            integer_object_nulls=False,
            date_as_object=True,
            ignore_metadata=True,
            categories=categories,
            safe=safe,
            types_mapper=_data_types.pyarrow2pandas_extension,
        ),
        dataset=dataset,
        path=path,
        path_root=path_root,
    )


def _read_parquet_chunked(
    paths: List[str],
    chunked: Union[bool, int],
    columns: Optional[List[str]],
    categories: Optional[List[str]],
    validate_schema: bool,
    safe: bool,
    boto3_session: boto3.Session,
    dataset: bool,
    path_root: Optional[str],
    s3_additional_kwargs: Optional[Dict[str, str]],
    use_threads: bool,
) -> Iterator[pd.DataFrame]:
    next_slice: Optional[pd.DataFrame] = None
    fs: s3fs.S3FileSystem = _utils.get_fs(
        s3fs_block_size=8_388_608, session=boto3_session, s3_additional_kwargs=s3_additional_kwargs  # 8 MB (8 * 2**20)
    )
    last_schema: Optional[Dict[str, str]] = None
    last_path: str = ""
    for path in paths:
        with _utils.open_file(fs=fs, path=path, mode="rb") as f:
            pq_file: pyarrow.parquet.ParquetFile = pyarrow.parquet.ParquetFile(source=f, read_dictionary=categories)
            schema: Dict[str, str] = _data_types.athena_types_from_pyarrow_schema(
                schema=pq_file.schema.to_arrow_schema(), partitions=None
            )[0]
            if validate_schema is True and last_schema is not None:
                if schema != last_schema:
                    raise exceptions.InvalidSchemaConvergence(
                        f"Was detect at least 2 different schemas:\n"
                        f"    - {last_path} -> {last_schema}\n"
                        f"    - {path} -> {schema}"
                    )
            last_schema = schema
            last_path = path
            num_row_groups: int = pq_file.num_row_groups
            _logger.debug("num_row_groups: %s", num_row_groups)
            for i in range(num_row_groups):
                _logger.debug("Reading Row Group %s...", i)
                df: pd.DataFrame = _arrowtable2df(
                    table=pq_file.read_row_group(
                        i=i, columns=columns, use_threads=use_threads, use_pandas_metadata=False
                    ),
                    categories=categories,
                    safe=safe,
                    use_threads=use_threads,
                    dataset=dataset,
                    path=path,
                    path_root=path_root,
                )
                if chunked is True:
                    yield df
                elif isinstance(chunked, int) and chunked > 0:
                    if next_slice is not None:
                        df = pd.concat(objs=[next_slice, df], ignore_index=True, sort=False, copy=False)
                    while len(df.index) >= chunked:
                        yield df.iloc[:chunked]
                        df = df.iloc[chunked:]
                    if df.empty:
                        next_slice = None
                    else:
                        next_slice = df
                else:
                    raise exceptions.InvalidArgument(f"chunked: {chunked}")
    if next_slice is not None:
        yield next_slice


def _read_parquet_file_single_thread(
    path: str,
    columns: Optional[List[str]],
    categories: Optional[List[str]],
    boto3_session: boto3.Session,
    s3_additional_kwargs: Optional[Dict[str, str]],
) -> pa.Table:
    fs: s3fs.S3FileSystem = _utils.get_fs(
        s3fs_block_size=134_217_728,
        session=boto3_session,
        s3_additional_kwargs=s3_additional_kwargs,  # 128 MB (128 * 2**20)
    )
    with _utils.open_file(fs=fs, path=path, mode="rb") as f:
        pq_file: pyarrow.parquet.ParquetFile = pyarrow.parquet.ParquetFile(source=f, read_dictionary=categories)
        return pq_file.read(columns=columns, use_threads=False, use_pandas_metadata=False)


def _count_row_groups(
    path: str,
    categories: Optional[List[str]],
    boto3_session: boto3.Session,
    s3_additional_kwargs: Optional[Dict[str, str]],
) -> int:
    fs: s3fs.S3FileSystem = _utils.get_fs(
        s3fs_block_size=4_194_304, session=boto3_session, s3_additional_kwargs=s3_additional_kwargs  # 4 MB (4 * 2**20)
    )
    with _utils.open_file(fs=fs, path=path, mode="rb") as f:
        pq_file: pyarrow.parquet.ParquetFile = pyarrow.parquet.ParquetFile(source=f, read_dictionary=categories)
        return pq_file.num_row_groups


def _read_parquet_file_multi_thread(
    row_group: int,
    path: str,
    columns: Optional[List[str]],
    categories: Optional[List[str]],
    boto3_primitives: _utils.Boto3PrimitivesType,
    s3_additional_kwargs: Optional[Dict[str, str]],
) -> pa.Table:
    boto3_session: boto3.Session = _utils.boto3_from_primitives(primitives=boto3_primitives)
    fs: s3fs.S3FileSystem = _utils.get_fs(
        s3fs_block_size=134_217_728,
        session=boto3_session,
        s3_additional_kwargs=s3_additional_kwargs,  # 128 MB (128 * 2**20)
    )
    with _utils.open_file(fs=fs, path=path, mode="rb") as f:
        pq_file: pyarrow.parquet.ParquetFile = pyarrow.parquet.ParquetFile(source=f, read_dictionary=categories)
        num_row_groups: int = pq_file.num_row_groups
        _logger.debug("Reading Row Group %s/%s [multi-threaded]", row_group + 1, num_row_groups)
        return pq_file.read_row_group(i=row_group, columns=columns, use_threads=False, use_pandas_metadata=False)


def _read_parquet_file(
    path: str,
    columns: Optional[List[str]],
    categories: Optional[List[str]],
    safe: bool,
    boto3_session: boto3.Session,
    dataset: bool,
    path_root: Optional[str],
    s3_additional_kwargs: Optional[Dict[str, str]],
    use_threads: bool,
) -> pd.DataFrame:
    if use_threads is False:
        table: pa.Table = _read_parquet_file_single_thread(
            path=path,
            columns=columns,
            categories=categories,
            boto3_session=boto3_session,
            s3_additional_kwargs=s3_additional_kwargs,
        )
    else:
        cpus: int = _utils.ensure_cpu_count(use_threads=use_threads)
        num_row_groups: int = _count_row_groups(
            path=path, categories=categories, boto3_session=boto3_session, s3_additional_kwargs=s3_additional_kwargs
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=cpus) as executor:
            tables: Tuple[pa.Table, ...] = tuple(
                executor.map(
                    _read_parquet_file_multi_thread,
                    range(num_row_groups),
                    itertools.repeat(path),
                    itertools.repeat(columns),
                    itertools.repeat(categories),
                    itertools.repeat(_utils.boto3_to_primitives(boto3_session=boto3_session)),
                    itertools.repeat(s3_additional_kwargs),
                )
            )
            table = pa.lib.concat_tables(tables, promote=False)
    _logger.debug("Converting PyArrow Table to Pandas DataFrame...")
    return _arrowtable2df(
        table=table,
        categories=categories,
        safe=safe,
        use_threads=use_threads,
        dataset=dataset,
        path=path,
        path_root=path_root,
    )


def read_parquet(
    path: Union[str, List[str]],
    path_suffix: Union[str, List[str], None] = None,
    path_ignore_suffix: Union[str, List[str], None] = None,
    partition_filter: Optional[Callable[[Dict[str, str]], bool]] = None,
    columns: Optional[List[str]] = None,
    validate_schema: bool = False,
    chunked: Union[bool, int] = False,
    dataset: bool = False,
    categories: Optional[List[str]] = None,
    safe: bool = True,
    use_threads: bool = True,
    last_modified_begin: Optional[datetime.datetime] = None,
    last_modified_end: Optional[datetime.datetime] = None,
    boto3_session: Optional[boto3.Session] = None,
    s3_additional_kwargs: Optional[Dict[str, str]] = None,
) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
    """Read Apache Parquet file(s) from from a received S3 prefix or list of S3 objects paths.

    The concept of Dataset goes beyond the simple idea of files and enable more
    complex features like partitioning and catalog integration (AWS Glue Catalog).

    Note
    ----
    ``Batching`` (`chunked` argument) (Memory Friendly):

    Will anable the function to return a Iterable of DataFrames instead of a regular DataFrame.

    There are two batching strategies on Wrangler:

    - If **chunked=True**, a new DataFrame will be returned for each file in your path/dataset.

    - If **chunked=INTEGER**, Wrangler will iterate on the data by number of rows igual the received INTEGER.

    `P.S.` `chunked=True` if faster and uses less memory while `chunked=INTEGER` is more precise
    in number of rows for each Dataframe.

    Note
    ----
    In case of `use_threads=True` the number of threads
    that will be spawned will be gotten from os.cpu_count().

    Note
    ----
    The filter by last_modified begin last_modified end is applied after list all S3 files

    Parameters
    ----------
    path : Union[str, List[str]]
        S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
    path_suffix: Union[str, List[str], None]
        Suffix or List of suffixes for filtering S3 keys.
    path_ignore_suffix: Union[str, List[str], None]
        Suffix or List of suffixes for S3 keys to be ignored.
    partition_filter: Optional[Callable[[Dict[str, str]], bool]]
        Callback Function filters to apply on PARTITION columns (PUSH-DOWN filter).
        This function MUST receive a single argument (Dict[str, str]) where keys are partitions
        names and values are partitions values.
        This function MUST return a bool, True to read the partition or False to ignore it.
        Ignored if `dataset=False`.
        E.g ``lambda x: True if x["year"] == "2020" and x["month"] == "1" else False``
    columns : List[str], optional
        Names of columns to read from the file(s).
    validate_schema:
        Check that individual file schemas are all the same / compatible. Schemas within a
        folder prefix should all be the same. Disable if you have schemas that are different
        and want to disable this check.
    chunked : Union[int, bool]
        If passed will split the data in a Iterable of DataFrames (Memory friendly).
        If `True` wrangler will iterate on the data by files in the most efficient way without guarantee of chunksize.
        If an `INTEGER` is passed Wrangler will iterate on the data by number of rows igual the received INTEGER.
    dataset: bool
        If `True` read a parquet dataset instead of simple file(s) loading all the related partitions as columns.
    categories: Optional[List[str]], optional
        List of columns names that should be returned as pandas.Categorical.
        Recommended for memory restricted environments.
    safe : bool, default True
        For certain data types, a cast is needed in order to store the
        data in a pandas DataFrame or Series (e.g. timestamps are always
        stored as nanoseconds in pandas). This option controls whether it
        is a safe cast or not.
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    last_modified_begin
        Filter the s3 files by the Last modified date of the object.
        The filter is applied only after list all s3 files.
    last_modified_end: datetime, optional
        Filter the s3 files by the Last modified date of the object.
        The filter is applied only after list all s3 files.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.
    s3_additional_kwargs:
        Forward to s3fs, useful for server side encryption
        https://s3fs.readthedocs.io/en/latest/#serverside-encryption

    Returns
    -------
    Union[pandas.DataFrame, Generator[pandas.DataFrame, None, None]]
        Pandas DataFrame or a Generator in case of `chunked=True`.

    Examples
    --------
    Reading all Parquet files under a prefix

    >>> import awswrangler as wr
    >>> df = wr.s3.read_parquet(path='s3://bucket/prefix/')

    Reading all Parquet files under a prefix encrypted with a KMS key

    >>> import awswrangler as wr
    >>> df = wr.s3.read_parquet(
    ...     path='s3://bucket/prefix/',
    ...     s3_additional_kwargs={
    ...         'ServerSideEncryption': 'aws:kms',
    ...         'SSEKMSKeyId': 'YOUR_KMY_KEY_ARN'
    ...     }
    ... )

    Reading all Parquet files from a list

    >>> import awswrangler as wr
    >>> df = wr.s3.read_parquet(path=['s3://bucket/filename0.parquet', 's3://bucket/filename1.parquet'])

    Reading in chunks (Chunk by file)

    >>> import awswrangler as wr
    >>> dfs = wr.s3.read_parquet(path=['s3://bucket/filename0.csv', 's3://bucket/filename1.csv'], chunked=True)
    >>> for df in dfs:
    >>>     print(df)  # Smaller Pandas DataFrame

    Reading in chunks (Chunk by 1MM rows)

    >>> import awswrangler as wr
    >>> dfs = wr.s3.read_parquet(path=['s3://bucket/filename0.csv', 's3://bucket/filename1.csv'], chunked=1_000_000)
    >>> for df in dfs:
    >>>     print(df)  # 1MM Pandas DataFrame

    """
    session: boto3.Session = _utils.ensure_session(session=boto3_session)
    paths: List[str] = _path2list(
        path=path,
        boto3_session=session,
        suffix=path_suffix,
        ignore_suffix=_get_path_ignore_suffix(path_ignore_suffix=path_ignore_suffix),
        last_modified_begin=last_modified_begin,
        last_modified_end=last_modified_end,
    )
    path_root: Optional[str] = _get_path_root(path=path, dataset=dataset)
    if path_root is not None:
        paths = _apply_partition_filter(path_root=path_root, paths=paths, filter_func=partition_filter)
    if len(paths) < 1:
        raise exceptions.NoFilesFound(f"No files Found on: {path}.")
    _logger.debug("paths:\n%s", paths)
    args: Dict[str, Any] = {
        "columns": columns,
        "categories": categories,
        "safe": safe,
        "boto3_session": session,
        "dataset": dataset,
        "path_root": path_root,
        "s3_additional_kwargs": s3_additional_kwargs,
        "use_threads": use_threads,
    }
    _logger.debug("args:\n%s", pprint.pformat(args))
    if chunked is not False:
        return _read_parquet_chunked(paths=paths, chunked=chunked, validate_schema=validate_schema, **args)
    if len(paths) == 1:
        return _read_parquet_file(path=paths[0], **args)
    if validate_schema is True:
        _validate_schemas_from_files(
            paths=paths,
            sampling=1.0,
            use_threads=True,
            boto3_session=boto3_session,
            s3_additional_kwargs=s3_additional_kwargs,
        )
    if use_threads is True:
        args["use_threads"] = True
        return _read_concurrent(func=_read_parquet_file, ignore_index=True, paths=paths, **args)
    return _union(dfs=[_read_parquet_file(path=p, **args) for p in paths], ignore_index=True)


def read_parquet_table(
    table: str,
    database: str,
    partition_filter: Optional[Callable[[Dict[str, str]], bool]] = None,
    columns: Optional[List[str]] = None,
    validate_schema: bool = True,
    categories: Optional[List[str]] = None,
    safe: bool = True,
    chunked: Union[bool, int] = False,
    use_threads: bool = True,
    boto3_session: Optional[boto3.Session] = None,
    s3_additional_kwargs: Optional[Dict[str, str]] = None,
) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
    """Read Apache Parquet table registered on AWS Glue Catalog.

    Note
    ----
    ``Batching`` (`chunked` argument) (Memory Friendly):

    Will anable the function to return a Iterable of DataFrames instead of a regular DataFrame.

    There are two batching strategies on Wrangler:

    - If **chunked=True**, a new DataFrame will be returned for each file in your path/dataset.

    - If **chunked=INTEGER**, Wrangler will paginate through files slicing and concatenating
      to return DataFrames with the number of row igual the received INTEGER.

    `P.S.` `chunked=True` if faster and uses less memory while `chunked=INTEGER` is more precise
    in number of rows for each Dataframe.


    Note
    ----
    In case of `use_threads=True` the number of threads
    that will be spawned will be gotten from os.cpu_count().

    Parameters
    ----------
    table : str
        AWS Glue Catalog table name.
    database : str
        AWS Glue Catalog database name.
    partition_filter: Optional[Callable[[Dict[str, str]], bool]]
        Callback Function filters to apply on PARTITION columns (PUSH-DOWN filter).
        This function MUST receive a single argument (Dict[str, str]) where keys are partitions
        names and values are partitions values.
        This function MUST return a bool, True to read the partition or False to ignore it.
        Ignored if `dataset=False`.
        E.g ``lambda x: True if x["year"] == "2020" and x["month"] == "1" else False``
    columns : List[str], optional
        Names of columns to read from the file(s).
    validate_schema:
        Check that individual file schemas are all the same / compatible. Schemas within a
        folder prefix should all be the same. Disable if you have schemas that are different
        and want to disable this check.
    categories: Optional[List[str]], optional
        List of columns names that should be returned as pandas.Categorical.
        Recommended for memory restricted environments.
    safe : bool, default True
        For certain data types, a cast is needed in order to store the
        data in a pandas DataFrame or Series (e.g. timestamps are always
        stored as nanoseconds in pandas). This option controls whether it
        is a safe cast or not.
    chunked : bool
        If True will break the data in smaller DataFrames (Non deterministic number of lines).
        Otherwise return a single DataFrame with the whole data.
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.
    s3_additional_kwargs:
        Forward to s3fs, useful for server side encryption
        https://s3fs.readthedocs.io/en/latest/#serverside-encryption

    Returns
    -------
    Union[pandas.DataFrame, Generator[pandas.DataFrame, None, None]]
        Pandas DataFrame or a Generator in case of `chunked=True`.

    Examples
    --------
    Reading Parquet Table

    >>> import awswrangler as wr
    >>> df = wr.s3.read_parquet_table(database='...', table='...')

    Reading Parquet Table encrypted

    >>> import awswrangler as wr
    >>> df = wr.s3.read_parquet_table(
    ...     database='...',
    ...     table='...'
    ...     s3_additional_kwargs={
    ...         'ServerSideEncryption': 'aws:kms',
    ...         'SSEKMSKeyId': 'YOUR_KMY_KEY_ARN'
    ...     }
    ... )

    Reading Parquet Table in chunks (Chunk by file)

    >>> import awswrangler as wr
    >>> dfs = wr.s3.read_parquet_table(database='...', table='...', chunked=True)
    >>> for df in dfs:
    >>>     print(df)  # Smaller Pandas DataFrame

    Reading in chunks (Chunk by 1MM rows)

    >>> import awswrangler as wr
    >>> dfs = wr.s3.read_parquet(path=['s3://bucket/filename0.csv', 's3://bucket/filename1.csv'], chunked=1_000_000)
    >>> for df in dfs:
    >>>     print(df)  # 1MM Pandas DataFrame

    """
    client_glue: boto3.client = _utils.client(service_name="glue", session=boto3_session)
    res: Dict[str, Any] = client_glue.get_table(DatabaseName=database, Name=table)
    try:
        path: str = res["Table"]["StorageDescriptor"]["Location"]
    except KeyError:
        raise exceptions.InvalidTable(f"Missing s3 location for {database}.{table}.")
    return _data_types.cast_pandas_with_athena_types(
        df=read_parquet(
            path=path,
            partition_filter=partition_filter,
            columns=columns,
            validate_schema=validate_schema,
            categories=categories,
            safe=safe,
            chunked=chunked,
            dataset=True,
            use_threads=use_threads,
            boto3_session=boto3_session,
            s3_additional_kwargs=s3_additional_kwargs,
        ),
        dtype=_extract_partitions_dtypes_from_table_details(response=res),
    )


def read_parquet_metadata(
    path: Union[str, List[str]],
    path_suffix: Optional[str] = None,
    path_ignore_suffix: Optional[str] = None,
    dtype: Optional[Dict[str, str]] = None,
    sampling: float = 1.0,
    dataset: bool = False,
    use_threads: bool = True,
    boto3_session: Optional[boto3.Session] = None,
    s3_additional_kwargs: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], Optional[Dict[str, str]]]:
    """Read Apache Parquet file(s) metadata from from a received S3 prefix or list of S3 objects paths.

    The concept of Dataset goes beyond the simple idea of files and enable more
    complex features like partitioning and catalog integration (AWS Glue Catalog).

    Note
    ----
    In case of `use_threads=True` the number of threads
    that will be spawned will be gotten from os.cpu_count().

    Parameters
    ----------
    path : Union[str, List[str]]
        S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
    path_suffix: Union[str, List[str], None]
        Suffix or List of suffixes for filtering S3 keys.
    path_ignore_suffix: Union[str, List[str], None]
        Suffix or List of suffixes for S3 keys to be ignored.
    dtype : Dict[str, str], optional
        Dictionary of columns names and Athena/Glue types to be casted.
        Useful when you have columns with undetermined data types as partitions columns.
        (e.g. {'col name': 'bigint', 'col2 name': 'int'})
    sampling : float
        Random sample ratio of files that will have the metadata inspected.
        Must be `0.0 < sampling <= 1.0`.
        The higher, the more accurate.
        The lower, the faster.
    dataset: bool
        If True read a parquet dataset instead of simple file(s) loading all the related partitions as columns.
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.
    s3_additional_kwargs:
        Forward to s3fs, useful for server side encryption
        https://s3fs.readthedocs.io/en/latest/#serverside-encryption

    Returns
    -------
    Tuple[Dict[str, str], Optional[Dict[str, str]]]
        columns_types: Dictionary with keys as column names and vales as
        data types (e.g. {'col0': 'bigint', 'col1': 'double'}). /
        partitions_types: Dictionary with keys as partition names
        and values as data types (e.g. {'col2': 'date'}).

    Examples
    --------
    Reading all Parquet files (with partitions) metadata under a prefix

    >>> import awswrangler as wr
    >>> columns_types, partitions_types = wr.s3.read_parquet_metadata(path='s3://bucket/prefix/', dataset=True)

    Reading all Parquet files metadata from a list

    >>> import awswrangler as wr
    >>> columns_types, partitions_types = wr.s3.read_parquet_metadata(path=[
    ...     's3://bucket/filename0.parquet',
    ...     's3://bucket/filename1.parquet'
    ... ])

    """
    return _read_parquet_metadata(
        path=path,
        path_suffix=path_suffix,
        path_ignore_suffix=path_ignore_suffix,
        dtype=dtype,
        sampling=sampling,
        dataset=dataset,
        use_threads=use_threads,
        s3_additional_kwargs=s3_additional_kwargs,
        boto3_session=_utils.ensure_session(session=boto3_session),
    )[:2]
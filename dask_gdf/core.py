import operator
from uuid import uuid4
from math import ceil

import numpy as np
import pandas as pd
import pygdf as gd
from toolz import merge

from dask.base import Base, tokenize, normalize_token
from dask.context import _globals
from dask.core import flatten
from dask.compatibility import apply
from dask.optimize import cull, fuse
from dask.threaded import get as threaded_get
from dask.utils import funcname
from dask.dataframe.utils import raise_on_meta_error
from dask.dataframe.core import Scalar

from .utils import make_meta


def optimize(dsk, keys, **kwargs):
    flatkeys = list(flatten(keys)) if isinstance(keys, list) else [keys]
    dsk, dependencies = cull(dsk, flatkeys)
    dsk, dependencies = fuse(dsk, keys, dependencies=dependencies,
                             ave_width=_globals.get('fuse_ave_width', 1))
    dsk, _ = cull(dsk, keys)
    return dsk


def finalize(results):
    return gd.concat(results)


class _Frame(Base):
    """ Superclass for DataFrame and Series

    Parameters
    ----------
    dsk : dict
        The dask graph to compute this DataFrame
    name : str
        The key prefix that specifies which keys in the dask comprise this
        particular DataFrame / Series
    meta : pygdf.DataFrame, pygdf.Series, or pygdf.Index
        An empty pygdf object with names, dtypes, and indices matching the
        expected output.
    divisions : tuple of index values
        Values along which we partition our blocks on the index
    """
    _default_get = staticmethod(threaded_get)
    _optimize = staticmethod(optimize)
    _finalize = staticmethod(finalize)

    def __init__(self, dsk, name, meta, divisions):
        self.dask = dsk
        self._name = name
        meta = make_meta(meta)
        if not isinstance(meta, self._partition_type):
            raise TypeError("Expected meta to specify type {0}, got type "
                            "{1}".format(self._partition_type.__name__,
                                         type(meta).__name__))
        self._meta = meta
        self.divisions = tuple(divisions)

    def _keys(self):
        return [(self._name, i) for i in range(self.npartitions)]

    def __repr__(self):
        s = "<dask_gdf.%s | %d tasks | %d npartitions>"
        return s % (type(self).__name__, len(self.dask), self.npartitions)

    @property
    def npartitions(self):
        """Return number of partitions"""
        return len(self.divisions) - 1

    @property
    def index(self):
        """Return dask Index instance"""
        name = self._name + '-index'
        dsk = {(name, i): (getattr, key, 'index')
               for i, key in enumerate(self._keys())}
        return Index(merge(dsk, self.dask), name,
                     self._meta.index, self.divisions)

    @classmethod
    def _get_unary_operator(cls, op):
        return lambda self: map_partitions(op, self)

    @classmethod
    def _get_binary_operator(cls, op, inv=False):
        if inv:
            return lambda self, other: map_partitions(op, other, self)
        else:
            return lambda self, other: map_partitions(op, self, other)

    def map_partitions(self, func, *args, **kwargs):
        """ Apply Python function on each DataFrame partition.

        Note that the index and divisions are assumed to remain unchanged.

        Parameters
        ----------
        func : function
            Function applied to each partition.
        args, kwargs :
            Arguments and keywords to pass to the function. The partition will
            be the first argument, and these will be passed *after*.
        """
        return map_partitions(func, self, *args, **kwargs)


normalize_token.register(_Frame, lambda a: a._name)


class DataFrame(_Frame):
    _partition_type = gd.DataFrame

    @property
    def columns(self):
        return self._meta.columns

    @property
    def dtypes(self):
        return self._meta.dtypes

    def __dir__(self):
        o = set(dir(type(self)))
        o.update(self.__dict__)
        o.update(c for c in self.columns if
                 (isinstance(c, pd.compat.string_types) and
                  pd.compat.isidentifier(c)))
        return list(o)

    def __getattr__(self, key):
        if key in self.columns:
            return self[key]
        raise AttributeError("'DataFrame' object has no attribute %r" % key)

    def __getitem__(self, key):
        if isinstance(key, str) and key in self.columns:
            meta = self._meta[key]
            name = 'getitem-%s' % tokenize(self, key)
            dsk = {(name, i): (operator.getitem, (self._name, i), key)
                   for i in range(self.npartitions)}
            return Series(merge(self.dask, dsk), name, meta, self.divisions)

        raise NotImplementedError("Indexing with %r" % key)


class Series(_Frame):
    _partition_type = gd.Series

    @property
    def dtype(self):
        return self._meta.dtype


for op in [operator.abs, operator.add, operator.eq, operator.gt, operator.ge,
           operator.lt, operator.le, operator.mod, operator.mul, operator.ne,
           operator.sub, operator.truediv, operator.floordiv]:
    Series._bind_operator(op)


class Index(Series):
    _partition_type = gd.index.Index

    @property
    def index(self):
        raise AttributeError("'Index' object has no attribute 'index'")


def splits_divisions_sorted_pygdf(df, chunksize):
    segments = df.index.find_segments()
    segments.append(len(df) - 1)

    splits = [0]
    last = current_size = 0
    for s in segments:
        size = s - last
        last = s
        current_size += size
        if current_size >= chunksize:
            splits.append(s)
            current_size = 0
    # Ensure end is included
    if splits[-1] != segments[-1]:
        splits.append(segments[-1])
    divisions = tuple(df.index.take(np.array(splits)).values)
    splits[-1] += 1  # Offset to extract to end

    return splits, divisions


def from_pygdf(data, npartitions=None, chunksize=None, sort=True, name=None):
    """Create a dask_gdf from a pygdf object

    Parameters
    ----------
    data : pygdf.DataFrame or pygdf.Series
    npartitions : int, optional
        The number of partitions of the index to create. Note that depending on
        the size and index of the dataframe, the output may have fewer
        partitions than requested.
    chunksize : int, optional
        The number of rows per index partition to use.
    sort : bool
        Sort input first to obtain cleanly divided partitions or don't sort and
        don't get cleanly divided partitions
    name : string, optional
        An optional keyname for the dataframe. Defaults to a uuid.

    Returns
    -------
    dask_gdf.DataFrame or dask_gdf.Series
        A dask_gdf DataFrame/Series partitioned along the index
    """
    if not isinstance(data, (gd.Series, gd.DataFrame)):
        raise TypeError("Input must be a pygdf DataFrame or Series")

    if ((npartitions is None) == (chunksize is None)):
        raise ValueError('Exactly one of npartitions and chunksize must '
                         'be specified.')

    nrows = len(data)

    if chunksize is None:
        chunksize = int(ceil(nrows / npartitions))

    name = name or ('from_pygdf-' + uuid4().hex)

    if sort:
        data = data.sort_index(ascending=True)
        splits, divisions = splits_divisions_sorted_pygdf(data, chunksize)
    else:
        splits = list(range(0, nrows, chunksize)) + [len(data)]
        divisions = (None,) * len(splits)

    dsk = {(name, i): data[start:stop]
           for i, (start, stop) in enumerate(zip(splits[:-1], splits[1:]))}

    return new_dd_object(dsk, name, data, divisions)


def _get_return_type(meta):
    if isinstance(meta, gd.Series):
        return Series
    elif isinstance(meta, gd.DataFrame):
        return DataFrame
    elif isinstance(meta, gd.Index):
        return Index
    return Scalar


def new_dd_object(dsk, name, meta, divisions):
    return _get_return_type(meta)(dsk, name, meta, divisions)


def _extract_meta(x):
    """
    Extract internal cache data (``_meta``) from dask_gdf objects
    """
    if isinstance(x, (Scalar, _Frame)):
        return x._meta
    elif isinstance(x, list):
        return [_extract_meta(_x) for _x in x]
    elif isinstance(x, tuple):
        return tuple([_extract_meta(_x) for _x in x])
    elif isinstance(x, dict):
        return {k: _extract_meta(v) for k, v in x.items()}
    return x


def _emulate(func, *args, **kwargs):
    """
    Apply a function using args / kwargs. If arguments contain dd.DataFrame /
    dd.Series, using internal cache (``_meta``) for calculation
    """
    with raise_on_meta_error(funcname(func)):
        return func(*_extract_meta(args), **_extract_meta(kwargs))


def align_partitions(args):
    """Align partitions between dask_gdf objects.

    Note that if all divisions are unknown, but have equal npartitions, then
    they will be passed through unchanged."""
    dfs = [df for df in args if isinstance(df, _Frame)]
    if not dfs:
        return args

    divisions = dfs[0].divisions
    if not all(df.divisions == divisions for df in dfs):
        raise NotImplementedError("Aligning mismatched partitions")
    return args


def map_partitions(func, *args, **kwargs):
    """ Apply Python function on each DataFrame partition.

    Parameters
    ----------
    func : function
        Function applied to each partition.
    args, kwargs :
        Arguments and keywords to pass to the function. At least one of the
        args should be a dask_gdf object.
    """
    meta = kwargs.pop('meta', None)
    if meta is not None:
        meta = make_meta(meta)

    if 'token' in kwargs:
        name = kwargs.pop('token')
        token = tokenize(meta, *args, **kwargs)
    else:
        name = funcname(func)
        token = tokenize(func, meta, *args, **kwargs)
    name = '{0}-{1}'.format(name, token)

    args = align_partitions(args)

    if meta is None:
        meta = _emulate(func, *args, **kwargs)
    meta = make_meta(meta)

    dfs = [df for df in args if isinstance(df, _Frame)]
    dsk = {}
    for i in range(dfs[0].npartitions):
        values = [(x._name, i if isinstance(x, _Frame) else 0)
                  if isinstance(x, (_Frame, Scalar)) else x for x in args]
        dsk[(name, i)] = (apply, func, values, kwargs)

    dasks = [arg.dask for arg in args if isinstance(arg, (_Frame, Scalar))]
    return new_dd_object(merge(dsk, *dasks), name, meta, args[0].divisions)

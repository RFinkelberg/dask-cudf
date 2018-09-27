"""

accessor.py contains classes for implementing
accessor properties.

"""

from toolz import partial
import pygdf as gd


class Accessor(object):
    """
    Base class for Accessor objects dt, str, cat.

    Notes
    -----
    Subclasses should define the following attributes:
    * _accessor
    * _accessor_name

    Subclasses should also implement the following methods:
    * _validate()

    """
    _not_implemented = frozenset([])

    def __init__(self, series):
        from .core import Series
        if not isinstance(series, Series):
            raise ValueError('Accessor cannot be initialized')
        self._series = series
        self._validate(series)

    def _validate(self, series):
        """ Validates the data type of series passed to the
        accessor.
        """
        raise NotImplementedError("Must implement")

    @staticmethod
    def _delegate_property(obj, accessor, attr):
        out = getattr(getattr(obj, accessor, obj), attr)
        return out

    @staticmethod
    def _delegate_method(obj, accessor, attr, args, kwargs):
        out = getattr(getattr(obj, accessor, obj), attr)(*args, **kwargs)
        return out

    def _property_map(self, attr):
        meta = self._delegate_property(self._series._meta,
                                       self._accessor_name, attr)
        token = '%s-%s' % (self._accessor_name, attr)
        return self._series.map_partitions(self._delegate_property,
                                           self._accessor_name, attr,
                                           token=token, meta=meta)

    def _function_map(self, attr, *args, **kwargs):
        meta = self._delegate_method(self._series._meta_nonempty,
                                     self._accessor_name, attr, args, kwargs)
        token = '%s-%s' % (self._accessor_name, attr)
        return self._series.map_partitions(self._delegate_method,
                                           self._accessor_name, attr, args,
                                           kwargs, meta=meta, token=token)

    @property
    def _delegates(self):
        return set(dir(self._accessor)).difference(self._not_implemented)

    def __dir__(self):
        o = self._delegates
        o.update(self.__dict__)
        o.update(dir(type(self)))
        return list(o)

    def __getattr__(self, key):
        if key in self._delegates:
            if isinstance(getattr(self._accessor, key), property):
                return self._property_map(key)
            else:
                return partial(self._function_map, key)
        else:
            raise AttributeError(key)

# Stolen from
# https://github.com/pandas-dev/pandas/blob/master/pandas/core/accessor.py


class CachedAccessor(object):
    """Custom property-like object (descriptor) for caching accessors.
    Parameters
    ----------
    name : str
        The namespace this will be accessed under, e.g. ``df.foo``
    accessor : cls
        The class with the extension methods. The class' __init__ method
        should expect one of a ``Series``, ``DataFrame`` or ``Index`` as
        the single argument ``data``
    """

    def __init__(self, name, accessor):
        self._name = name
        self._accessor = accessor

    def __get__(self, obj, cls):
        if obj is None:
            # we're accessing the attribute of the class, i.e., Dataset.geo
            return self._accessor
        accessor_obj = self._accessor(obj)
        # Replace the property with the accessor object. Inspired by:
        # http://www.pydanny.com/cached-property.html
        # We need to use object.__setattr__ because we overwrite __setattr__ on
        # NDFrame
        # object.__setattr__(obj, self._name, accessor_obj)
        return accessor_obj


class DatetimeAccessor(Accessor):
    """ Accessor object for datetimelike properties of the Series values.
    """

    from pygdf.series import DatetimeProperties

    _accessor = DatetimeProperties
    _accessor_name = 'dt'

    def _validate(self, series):
        if not isinstance(series._meta._column, gd.datetime.DatetimeColumn):
            raise AttributeError("Can only use .dt accessor with datetimelike "
                                 "values")


class CategoricalAccessor(Accessor):
    """ Accessor object for categorical properties of the Series values
    of Categorical type.
    """

    from pygdf.categorical import CategoricalAccessor as GdfCategoricalAccessor

    _accessor = GdfCategoricalAccessor
    _accessor_name = 'cat'

    def _validate(self, series):
        if not isinstance(series._meta._column,
                          gd.categorical.CategoricalColumn):
            raise AttributeError(
                "Can only use .cat accessor with categorical values")

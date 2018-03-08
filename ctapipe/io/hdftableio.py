import re
from abc import abstractmethod, ABCMeta
from collections import defaultdict
from functools import partial

import numpy as np
import tables
from astropy.time import Time
from astropy.units import Quantity

import ctapipe
from ctapipe.core import Component, Container

__all__ = ['TableWriter',
           'TableReader',
           'HDF5TableWriter',
           'HDF5TableReader']

PYTABLES_TYPE_MAP = {
    'float': tables.Float64Col,
    'float64': tables.Float64Col,
    'float32': tables.Float32Col,
    'int': tables.IntCol,
    'int32': tables.Int32Col,
    'int64': tables.Int64Col,
    'bool': tables.BoolCol,
}


class TableWriter(Component, metaclass=ABCMeta):

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        self._transforms = defaultdict(dict)
        self._exclusions = defaultdict(list)

    def __del__(self):

        self.close()

    def __enter__(self):

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):

        self.close()

    def exclude(self, table_name, pattern):
        """
        Exclude any columns matching the pattern from being written

        Parameters
        ----------
        table_name: str
            name of table on which to apply the exclusion
        pattern: str
            regular expression string to match column name
        """
        self._exclusions[table_name].append(re.compile(pattern))

    def _is_column_excluded(self, table_name, col_name):
        for pattern in self._exclusions[table_name]:
            if pattern.match(col_name):
                return True
        return False

    def add_column_transform(self, table_name, col_name, transform):
        """
        Add a transformation function for a column. This function will be
        called on the value in the container before it is written to the
        output file.

        Parameters
        ----------
        table_name: str
            identifier of table being written
        col_name: str
            name of column in the table (or item in the Container)
        transform: callable
            function that take a value and returns a new one
        """
        self._transforms[table_name][col_name] = transform
        self.log.debug("Added transform: {}/{} -> {}".format(table_name,
                                                             col_name,
                                                             transform))

    @abstractmethod
    def write(self, table_name, containers):
        """
        Write the contents of the given container or containers to a table.
        The first call to write  will create a schema and initialize the table
        within the file.
        The shape of data within the container must not change between calls,
        since variable-length arrays are not supported.

        Parameters
        ----------
        table_name: str
            name of table to write to
        container: `ctapipe.core.Container`
            container to write
        """
        pass

    @abstractmethod
    def open(self, filename):

        pass

    @abstractmethod
    def close(self):

        pass

    def _apply_col_transform(self, table_name, col_name, value):
        """
        apply value transform function if it exists for this column
        """
        if col_name in self._transforms[table_name]:
            tr = self._transforms[table_name][col_name]
            value = tr(value)
        return value


class HDF5TableWriter(TableWriter):
    """
    A very basic table writer that can take a container (or more than one)
    and write it to an HDF5 file. It does _not_ recursively write the
    container. This is intended as a building block to create a more complex
    I/O system.

    It works by creating a HDF5 Table description from the `Field`s inside a
    container, where each item becomes a column in the table. The first time
    `SimpleHDF5TableWriter.write()` is called, the container is registered
    and the table created in the output file.

    Each item in the container can also have an optional transform function
    that is called before writing to transform the value.  For example,
    unit quantities always have their units removed, or converted to a
    common unit if specified in the `Field`.

    Any metadata in the `Container` (stored in `Container.meta`) will be
    written to the table's header on the first call to write()

    Multiple tables may be written at once in a single file, as long as you
    change the table_name attribute to write() to specify which one to write
    to.

    TODO:
    - ability to write several containers to the same table (appending a
    string to each column name). Perhaps `write(name, dict(method_a=cont,
    method_b=cont2))`, where "method_a_X" would be a column name. May be
    possible to do this with some container magic, like joining two
    containers `joined_container(cont1, cont2, "A", "B")` or "cont1+cont2".
    Perhaps need to provide a better way to get container contents as a
    dictionary.

    Parameters
    ----------
    filename: str
        name of hdf5 output file
    group_name: str
        name of group into which to put all of the tables generated by this
        Writer (it will be placed under "/" in the file)

    """

    def __init__(self, filename, group_name, **kwargs):
        super().__init__()
        self._schemas = {}
        self._tables = {}
        self._h5file = self.open(filename, **kwargs)
        self._group = self._h5file.create_group("/", group_name)
        self.log.debug("h5file: {}".format(self._h5file))

    def __del__(self):
        self.close()

    def open(self, filename, **kwargs):

        self._h5file = tables.open_file(filename, mode="w", **kwargs)

        return self._h5file

    def close(self):

        self._h5file.close()

    def _create_hdf5_table_schema(self, table_name, containers):
        """
        Creates a pytables description class for the given containers
        and registers it in the Writer

        Parameters
        ----------
        table_name: str
            name of table
        container: ctapipe.core.Container
            instance of an initalized container

        Returns
        -------
        dictionary of extra metadata to add to the table's header
        """

        class Schema(tables.IsDescription):
            pass

        meta = {}  # any extra meta-data generated here (like units, etc)

        # create pytables schema description for the given container
        for container in containers:
            for col_name, value in container.items():

                typename = ""
                shape = 1

                if self._is_column_excluded(table_name, col_name):
                    self.log.debug("excluded column: {}/{}".format(
                        table_name, col_name
                    ))
                    continue

                if isinstance(value, Quantity):
                    req_unit = container.fields[col_name].unit
                    if req_unit is not None:
                        tr = partial(tr_convert_and_strip_unit, unit=req_unit)
                        meta['{}_UNIT'.format(col_name)] = str(req_unit)
                    else:
                        tr = lambda x: x.value
                        meta['{}_UNIT'.format(col_name)] = str(value.unit)

                    value = tr(value)
                    self.add_column_transform(table_name, col_name, tr)

                if isinstance(value, np.ndarray):
                    typename = value.dtype.name
                    coltype = PYTABLES_TYPE_MAP[typename]
                    shape = value.shape
                    Schema.columns[col_name] = coltype(shape=shape)

                if isinstance(value, Time):
                    # TODO: really should use MET, but need a func for that
                    Schema.columns[col_name] = tables.Float64Col()
                    self.add_column_transform(
                        table_name, col_name, tr_time_to_float
                    )

                elif type(value).__name__ in PYTABLES_TYPE_MAP:
                    typename = type(value).__name__
                    coltype = PYTABLES_TYPE_MAP[typename]
                    Schema.columns[col_name] = coltype()

                self.log.debug(
                    "Table {}: added col: {} type: {} shape: {}".format(
                        table_name, col_name, typename, shape
                    ))

        self._schemas[table_name] = Schema
        meta['CTAPIPE_VERSION'] = ctapipe.__version__
        return meta

    def _setup_new_table(self, table_name, containers):
        """ set up the table. This is called the first time `write()`
        is called on a new table """
        self.log.debug("Initializing table '{}'".format(table_name))
        meta = self._create_hdf5_table_schema(table_name, containers)

        for container in containers:
            meta.update(container.meta)  # copy metadata from container

        table = self._h5file.create_table(
            where=self._group,
            name=table_name,
            title="Storage of {}".format(
                ",".join(c.__class__.__name__ for c in containers)
            ),
            description=self._schemas[table_name]
        )
        for key, val in meta.items():
            table.attrs[key] = val

        self._tables[table_name] = table

    def _append_row(self, table_name, containers):
        """
        append a row to an already initialized table. This is called
        automatically by `write()`
        """
        table = self._tables[table_name]
        row = table.row

        for container in containers:
            for colname in filter(lambda c: c in table.colnames, container.keys()):
                value = self._apply_col_transform(
                    table_name, colname, container[colname]
                )

                row[colname] = value

        row.append()

    def write(self, table_name, containers):
        """
        Write the contents of the given container or containers to a table.
        The first call to write  will create a schema and initialize the table
        within the file.
        The shape of data within the container must not change between
        calls, since variable-length arrays are not supported.

        Parameters
        ----------
        table_name: str
            name of table to write to
        containers: `ctapipe.core.Container` or `Iterable[ctapipe.core.Container]`
            container to write
        """
        if isinstance(containers, Container):
            containers = (containers, )

        if table_name not in self._schemas:
            self._setup_new_table(table_name, containers)

        self._append_row(table_name, containers)


class TableReader(Component, metaclass=ABCMeta):
    """
    Base class for row-wise table readers. Generally methods that read a
    full table at once are preferred to this method, since they are faster,
    but this can be used to re-play a table row by row into a
    `ctapipe.core.Container` class (the opposite of TableWriter)
    """

    def __init__(self):
        super().__init__()
        self._cols_to_read = defaultdict(list)
        self._transforms = defaultdict(dict)

    def __del__(self):

        self.close()

    def __enter__(self):

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):

        self.close()

    def add_column_transform(self, table_name, col_name, transform):
        """
        Add a transformation function for a column. This function will be
        called on the value in the container before it is written to the
        output file.

        Parameters
        ----------
        table_name: str
            identifier of table being written
        col_name: str
            name of column in the table (or item in the Container)
        transform: callable
            function that take a value and returns a new one
        """
        self._transforms[table_name][col_name] = transform
        self.log.debug("Added transform: {}/{} -> {}".format(table_name,
                                                             col_name,
                                                             transform))

    def _apply_col_transform(self, table_name, col_name, value):
        """
        apply value transform function if it exists for this column
        """
        if col_name in self._transforms[table_name]:
            tr = self._transforms[table_name][col_name]
            value = tr(value)
        return value

    @abstractmethod
    def read(self, table_name, container):
        """
        Returns a generator that reads the next row from the table into the
        given container.  The generator returns the same container. Note that
        no containers are copied, the data are overwritten inside.
        """
        pass

    @abstractmethod
    def open(self, filename):

        pass

    @abstractmethod
    def close(self):

        pass


class HDF5TableReader(TableReader):
    """
    Reader that reads a single row of an HDF5 table at once into a Container.
    Simply construct a `HDF5TableReader` with an input HDF5 file,
    and call the `read(path, container)` method to get a generator that fills
    the given container with a new row of the table on each access.

    Columns in the table are automatically mapped to container fields by
    name, and if a field is missing in either, it is skipped during read,
    but a warning is emitted.

    Columns that were written by SimpleHDF5TableWriter and which had unit
    transforms applied, will have the units re-applied when reading (the
    unit used is stored in the header attributes).

    Note that this is only useful if you want to read all information *one
    event at a time* into a container, which is not very I/O efficient. For
    some other use cases, it may be much more efficient to access the
    table data directly, for example to read an entire column or table at
    once (which means not using the Container data structure).

    Todo:
    - add ability to synchronize reading of multiple tables on a key

    - add ability (also with TableWriter) to read a row into n containers at
        once, assuming no naming conflicts (so we can add e.g. event_id)

    """

    def __init__(self, filename):
        """
        Parameters
        ----------
        filename: str
            name of hdf5 file
        group_name: str
            HDF5 path to group where tables are  to be found
        """
        super().__init__()
        self._tables = {}
        self._h5file = self.open(filename)
        pass

    def open(self, filename):

        self._h5file = tables.open_file(filename)
        return self._h5file

    def close(self):

        self._h5file.close()

    def _setup_table(self, table_name, container):
        tab = self._h5file.get_node(table_name)
        self._tables[table_name] = tab
        self._map_table_to_container(table_name, container)
        self._map_transforms_from_table_header(table_name)
        return tab

    def _map_transforms_from_table_header(self, table_name):
        """
        create any transforms needed to "undo" ones in the writer
        """
        tab = self._tables[table_name]
        for attr in tab.attrs._f_list():
            if attr.endswith("_UNIT"):
                colname = attr[:-5]
                tr = partial(tr_add_unit, unitname=tab.attrs[attr])
                self.add_column_transform(table_name, colname, tr)

    def _map_table_to_container(self, table_name, container):
        """ identifies which columns in the table to read into the container,
        by comparing their names."""
        tab = self._tables[table_name]
        for colname in tab.colnames:
            if colname in container.fields:
                self._cols_to_read[table_name].append(colname)
            else:
                self.log.warn("Table '{}' has column '{}' that is not in "
                              "container {}. It will be skipped"
                              .format(table_name, colname,
                                      container.__class__.__name__))

        # also check that the container doesn't have fields that are not
        # in the table:
        for colname in container.fields:
            if colname not in self._cols_to_read[table_name]:
                self.log.warn("Table '{}' is missing column '{}' that is "
                              "in container {}. It will be skipped"
                              .format(table_name, colname,
                                      container.__class__.__name__))

        # copy all user-defined attributes back to Container.mets
        for key in tab.attrs._f_list():
            container.meta[key] = tab.attrs[key]

    def read(self, table_name, container):
        """
        Returns a generator that reads the next row from the table into the
        given container.  The generator returns the same container. Note that
        no containers are copied, the data are overwritten inside.
        """
        if table_name not in self._tables:
            tab = self._setup_table(table_name, container)
        else:
            tab = self._tables[table_name]

        row_count = 0

        while 1:

            try:
                row = tab[row_count]
            except IndexError:
                return  # stop generator when done

            for colname in self._cols_to_read[table_name]:
                container[colname] = self._apply_col_transform(table_name,
                                                               colname,
                                                               row[colname])

            yield container
            row_count += 1


def tr_convert_and_strip_unit(quantity, unit):
    return quantity.to(unit).value


def tr_list_to_mask(thelist, length):
    """ transform list to a fixed-length mask"""
    arr = np.zeros(shape=length, dtype=np.bool)
    arr[thelist] = True
    return arr


def tr_time_to_float(thetime):
    return thetime.mjd


def tr_add_unit(value, unitname):
    return Quantity(value, unitname)

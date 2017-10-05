# Copyright (c) 2017 The Regents of the University of Michigan
# All rights reserved.
# This software is licensed under the BSD 3-Clause License.
"""Synchronization of jobs and projects.

Jobs may be synchronized by copying all data from the source job to the
destination job. This means all files are copied and the documents
are synchronized. Conflicts, that means both jobs contain conflicting
data, may be resolved with a user defined strategy.

The synchronization of projects is in essence the synchronization of all jobs
which are in the destination project with the ones in the source project and
the sync synchronization of the project document. If a specific job does not
exist yet at the destination it is simply cloned, otherwise it is synchronized.

A sync strategy is a function (or functor) that takes the source job,
the destination job, and the name of the file generating the conflict
as arguments and returns the decision whether to overwrite the file as
Boolean. There are some default strategies defined within this module as
part of the :class:`~.FileSync` class. These are the default strategies:

    1. always -- Always overwrite on conflict.
    2. never -- Never overwrite on conflict.
    3. time -- Overwrite when the modification time of the source file is newer.
    4. Ask -- Ask the user interactively about each conflicting filename.

For example, to synchronize two projects resolving conflicts by modification time, use:

.. code-block:: python

    dest_project.sync(source_project, strategy=sync.FileSync.time)

Unlike files, which are always either overwritten as a whole or not, documents
can be synchronized more fine-grained with a *sync function*. Such a function (or
functor) takes the source and the destination document as arguments and performs
the synchronization. The user is encouraged to implement their own sync functions,
but there are a few default functions implemented as part of the :class:`~.DocSync` class:

    1. NO_SYNC -- Do not perform any synchronization.
    2. COPY -- Apply the same strategy used to resolve file conflicts.
    3. update -- Equivalent to dst.update(src).
    4. ByKey -- Synchronize the source document key by key, more information below.

This is how we could synchronize two jobs, where the documents are synchronized
with a simple update function:

.. code-block:: python

    dst_job.sync(src_job, doc_sync=sync.DocSync.update)

The :class:`.DocSync.ByKey` functor attempts to synchronize the destination document
with the source document without overwriting any data. That means this function
behaves similar to :func:`~.DocSync.update` for a non-intersecting set of keys,
but in addition will preserve nested mappings without overwriting values. In addition,
any key conflict, that means keys that are present in both documents, but have
differing data, will lead to the raise of a :class:`.DocumentSyncConflict` exception.
The user may expclitly decide to overwrite certain keys by providing a "key-strategy",
which is a function that takes the conflicting key as argument, and returns the
decision whether to overwrite that specific key as Boolean. For example, to sync
two jobs, where conflicting keys should only be overwritten if they contain the
term 'foo', we could execute:

.. code-block:: python

    dst_job.sync(src_job, doc_sync=sync.DocSync.ByKey(lambda key: 'foo' in key))

This means that all documents are synchronized 'key-by-key' and only conflicting keys that
contain the word "foo" will be overwritten, any other conflicts would lead to the
raise of a :class:`~.DocumentSyncConflict` exception. A key-strategy may also be
a regular expression, so the synchronization above could also be achieved with:

.. code-block:: python

    dst_job.sync(src_job, doc_sync=sync.DocSync.ByKey('foo'))
"""
import os
import re
import shutil
import filecmp
import logging
from copy import deepcopy
from contextlib import contextmanager
from collections import defaultdict as ddict
from multiprocessing.pool import ThreadPool

from .errors import DestinationExistsError
from .errors import FileSyncConflict
from .errors import DocumentSyncConflict
from .errors import SchemaSyncConflict
from .contrib.utility import query_yes_no
from .common import six
from filecmp import dircmp
if six.PY2:
    from collections import Mapping
else:
    from collections.abc import Mapping


LEVEL_MORE = logging.INFO - 5

logger = logging.getLogger(__name__)
logging.addLevelName(LEVEL_MORE, 'MORE')
logging.MORE = LEVEL_MORE


def log_more(msg, *args, **kwargs):
    logger.log(LEVEL_MORE, msg, *args, **kwargs)


logger.more = log_more


__all__ = [
    'FileSync',
    'DocSync',
    'sync_jobs',
    'sync_projects',
]


class dircmp_deep(dircmp):

    def phase3(self):  # Find out differences between common files
        xx = filecmp.cmpfiles(self.left, self.right, self.common_files, shallow=False)
        self.same_files, self.diff_files, self.funny_files = xx

    methodmap = dict(dircmp.methodmap)
    methodmap['samefiles'] = methodmap['diff_files'] = phase3


class _DocProxy(object):
    """Proxy object for document (mapping) modifications.

    This proxy is used to keep track of changes and ensure that
    dry runs do not actually modify any data.

    :param dry_run:
        Do not actually perform any data modification operation, but
        still log the action.
    :type dry_run:
        bool
    """

    def __init__(self, doc, dry_run=False):
        self.doc = doc
        self.dry_run = dry_run

    def __str__(self):
        return "_DocProxy({})".format(str(self.doc))

    def __repr__(self):
        return "_DocProxy({})".format(repr(self.doc))

    def __getitem__(self, key):
        return self.doc[key]

    def __setitem__(self, key, value):
        logger.more("Set '{}'='{}'.".format(key, value))
        if not self.dry_run:
            self.doc[key] = value

    def keys(self):
        return self.doc.keys()

    def clear(self):
        self.doc.clear()

    def update(self, other):
        for key in other.keys():
            self[key] = other[key]

    def __iter__(self):
        return iter(self.doc)

    def __contains__(self, key):
        return key in self.doc

    def __eq__(self, other):
        return self.doc.__eq__(other)

    def __len__(self):
        return len(self.doc)


class _FileModifyProxy(object):
    """This proxy used for data modification.

    This proxy is used for all file data modification to keep
    track of changes and to ensure that dry runs do not actually
    modify any data.

    :param dry_run:
        Do not actually perform any data modification operation, but
        still log the action.
    :type dry_run:
        bool
    """

    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    def copy(self, src, dst):
        logger.more("Copy file '{}' -> '{}'.".format(os.path.relpath(src), os.path.relpath(dst)))
        if not self.dry_run:
            shutil.copy(src, dst)

    def copytree(self, src, dst):
        logger.more("Copy tree '{}' -> '{}'.".format(os.path.relpath(src), os.path.relpath(dst)))
        if not self.dry_run:
            shutil.copytree(src, dst)

    def remove(self, path):
        logger.more("Remove path '{}'.".format(os.path.relpath(path)))
        if not self.dry_run:
            os.remove(path)

    @contextmanager
    def create_backup(self, path):
        logger.debug("Create backup of '{}'.".format(os.path.relpath(path)))
        path_backup = path + '~'
        if os.path.isfile(path_backup):
            raise RuntimeError(
                "Failed to create backup, file already exists: '{}'.".format(
                    os.path.relpath(path_backup)))
        try:
            self.copy(path, path_backup)
            yield path_backup
        except:
            logger.more("Error occured, restoring backup...")
            self.copy(path_backup, path)
            raise
        finally:
            logger.debug("Remove backup of '{}'.".format(os.path.relpath(path)))
            self.remove(path_backup)

    @contextmanager
    def create_doc_backup(self, doc):
        proxy = _DocProxy(doc, dry_run=self.dry_run)
        fn = getattr(doc, 'filename', getattr(doc, '_filename', None))
        if not len(proxy) or fn is None or not os.path.isfile(fn):
            backup = deepcopy(doc)  # use in-memory backup
            try:
                yield proxy
            except:     # roll-back
                proxy.clear()
                proxy.update(backup)
                raise
        else:
            with self.create_backup(fn):
                yield proxy


# Definition of default sync strategies

class FileSync(object):
    "Collection of file synchronization strategies."

    @classmethod
    def keys(cls):
        return ('always', 'never', 'update', 'Ask')

    @staticmethod
    def always(src, dst, fn):
        "Always overwrite files on conflict."
        return True

    @staticmethod
    def never(src, dst, fn):
        "Never overwrite files on conflict."
        return False

    @staticmethod
    def update(src, dst, fn):
        "Overwrite a file if the source file was modified last (based on timestamp)."
        return os.path.getmtime(src.fn(fn)) > os.path.getmtime(dst.fn(fn))

    class Ask(object):
        "Ask whether a file should be overwritten interactively."

        def __init__(self):
            self.yes = set()
            self.no = set()

        def __call__(self, src, dst, fn):
            if fn in self.yes:
                return True
            elif fn in self.no:
                return False
            else:
                overwrite = query_yes_no("Overwrite files named '{}'?".format(fn), 'no')
                if overwrite:
                    self.yes.add(fn)
                    return True
                else:
                    self.no.add(fn)
                    return False


class DocSync(object):
    "Collection of document synchronization functions."

    NO_SYNC = False
    "Do not synchronize documents."

    COPY = 'copy'
    "Copy (and potentially overwrite) documents like any other file."

    @staticmethod
    def update(src, dst):
        "Perform a simple update."
        for key in src.keys():
            dst[key] = src[key]

    class ByKey(object):
        "Synchronize documents key by key."

        def __init__(self, key_strategy=None):
            if isinstance(key_strategy, str):

                def regex_key_strategy(key):
                    return re.match(key_strategy, key)

                self.key_strategy = regex_key_strategy
            else:
                self.key_strategy = key_strategy
            self.skipped_keys = set()

        def __str__(self):
            return "{}({})".format(type(self).__name__, self.key_strategy)

        def __call__(self, src, dst, root=''):
            if src == dst:
                return
            for key, value in src.items():
                if key in dst:
                    if dst[key] == value:
                        continue
                    elif isinstance(value, Mapping):
                        self(src[key], dst[key], key + '.')
                        continue
                    elif self.key_strategy is None or not self.key_strategy(root + key):
                        self.skipped_keys.add(root + key)
                        continue
                dst[key] = value

            # Check for skipped keys and raise an exception in case that no strategy
            # was provided, otherwise just log them.
            if self.skipped_keys and not root:
                if self.key_strategy is None:
                    raise DocumentSyncConflict(self.skipped_keys)
                else:
                    logger.more("Skipped keys: {}".format(', '.join(self.skipped_keys)))


def _sync_job_workspaces(src, dst, strategy, exclude, proxy, subdir='', deep=False):
    "Synchronize two job workspaces file by file, following the provided strategy."
    if deep:
        diff = dircmp_deep(src.fn(subdir), dst.fn(subdir))
    else:
        diff = dircmp(src.fn(subdir), dst.fn(subdir))

    for fn in diff.left_only:
        if exclude and any([re.match(p, fn) for p in exclude]):
            logger.debug("File named '{}' is skipped (excluded).".format(fn))
            continue
        fn_src = os.path.join(src.workspace(), subdir, fn)
        fn_dst = os.path.join(dst.workspace(), subdir, fn)
        if os.path.isfile(fn_src):
            proxy.copy(fn_src, fn_dst)
        else:
            proxy.copytree(fn_src, fn_dst)
    for fn in diff.diff_files:
        if exclude and any([re.match(p, fn) for p in exclude]):
            logger.debug("File named '{}' is skipped (excluded).".format(fn))
            continue
        if strategy is None:
            raise FileSyncConflict(fn)
        else:
            fn_src = os.path.join(src.workspace(), subdir, fn)
            fn_dst = os.path.join(dst.workspace(), subdir, fn)
            if strategy(src, dst, os.path.join(subdir, fn)):
                proxy.copy(fn_src, fn_dst)
            else:
                logger.debug("Skip file '{}'.".format(fn))
    for _subdir in diff.subdirs:
        _sync_job_workspaces(
            src, dst, strategy, exclude, proxy, os.path.join(subdir, _subdir), deep=deep)


def _identical_path(a, b):
    return os.path.abspath(os.path.realpath(a)) == os.path.abspath(os.path.realpath(b))


def sync_jobs(src, dst, strategy=None, exclude=None, doc_sync=None, dry_run=False, deep=False):
    """Synchronize the src job with the dst job.

        By default, this method will synchronize all files and document data
        of dst job with the src job until a synchronization conflict occurs.
        There are two different kinds of synchronization conflicts:

            1. The two jobs have files with the same name, but different content.
            2. The two jobs have documents that share keys, but those keys are
               mapped to different values.

        A file conflict can be resolved by providing a 'FileSync' *strategy* or by
        *excluding* files from the synchronization. An unresolvable conflict is indicated
        with the raise of a :class:`~.errors.FileSyncConflict` exception.

        A document synchronization conflict can be resolved by providing a doc_sync function
        that takes the source and the destination document as first and second argument.

        :param src:
            The src job, data will be copied from this job's workspace.
        :type src:
            `~.Job`
        :param dst:
            The dst job, data will be copied to this job's workspace.
        :type dst:
            `~.Job`
        :param strategy:
            A synchronization strategy for file conflicts. If no strategy is provided,
            a :class:`.errors.SyncConflict` exception will be raised upon conflict.
        :param exclude:
            A filename exclusion pattern. All files matching this pattern will be
            excluded from the synchronization process.
        :type exclude:
            str
        :param doc_sync:
            A synchronization strategy for document keys. The default is to use a safe key-by-key
            strategy that will not overwrite any values on conflict, but instead raises a
            :class:`~.errors.DocumentSyncConflict` exception.
        :param dry_run:
            If True, do not actually perform any synchronization operations.
    """
    # Check identity
    if _identical_path(src.workspace(), dst.workspace()):
        raise ValueError("Source and destination can't be the same!")

    # check src and dst compatiblity
    assert src.FN_MANIFEST == dst.FN_MANIFEST
    assert src.FN_DOCUMENT == dst.FN_DOCUMENT

    # Nothing to be done if the src is not initialized.
    if src not in src._project:
        return

    # The doc_sync functions defaults to a safe "by_key" strategy.
    if doc_sync is None:
        doc_sync = DocSync.ByKey()

    # the exclude argument must be a list
    if exclude is None:
        exclude = []
    elif not isinstance(exclude, list):
        exclude = [exclude]
    exclude.append(src.FN_MANIFEST)
    if doc_sync != DocSync.COPY:
        exclude.append(src.FN_DOCUMENT)

    if type(dry_run) == _FileModifyProxy:
        proxy = dry_run
    else:
        proxy = _FileModifyProxy(dry_run=bool(dry_run))
    if proxy.dry_run:
        logger.debug("Synchronizing job '{}' (dry run)...".format(src))
    else:
        logger.debug("Synchronizing job '{}'...".format(src))

    if os.path.isdir(src.workspace()):
        dst.init()
        _sync_job_workspaces(src, dst, strategy, exclude, proxy, deep=deep)

    if not (doc_sync is DocSync.NO_SYNC or doc_sync == DocSync.COPY):
        with proxy.create_doc_backup(dst.document) as dst_proxy:
            doc_sync(src.document, dst_proxy)


def sync_projects(source, destination, strategy=None, exclude=None, doc_sync=None,
                  selection=None, check_schema=True, dry_run=False, parallel=False):
    """Synchronize the destination project with the source project.

    Try to clone all jobs from the source to the destination.
    If the destination job already exist, try to synchronize the job using the
    optionally specified strategy.

    :param source:
        The project presenting the source for synchronization.
    :type source:
        :class:`~.Project`
    :param destination:
        The project that is modified for synchronization.
    :type destination:
        :class:`~.Project`
    :param strategy:
        A file synchronization strategy.
    :param exclude:
        Files with names matching the given pattern will be excluded
        from the synchronization.
    :param doc_sync:
        The function applied for synchronizing documents.
    :param selection:
        Only synchronize the given selection of jobs.
    :param check_schema:
        If True, only synchronize if this and the other project have a matching
        state point schema. See also: :meth:`~.detect_schema`.
    :type check_schema:
        bool
    :param dry_run:
        If True (the default), do not actually perform the synchronization operation,
        just log what would happen theoretically. Useful to test synchronization strategies
        without the risk of data loss.
    :type dry_run:
        bool
    :raises DocumentSyncConflict:
        If there are conflicting keys within the project or job documents that cannot
        be resolved with the given strategy or if there is no strategy provided.
    :raises FileSyncConflict:
        If there are differing files that cannot be resolved with the given strategy
        or if no strategy is provided.
    :raises SyncSchemaConflict:
        In case that the check_schema argument is True and the detected state point
        schema of this and the other project differ.
    """
    if source == destination:
        raise ValueError("Source and destination project cannot be identical!")

    # Setup data modification proxy
    proxy = _FileModifyProxy(dry_run=dry_run)

    # Perform a schema check in an attempt to avoid bad sync operations.
    if check_schema:
        schema_src = source.detect_schema()
        schema_dst = destination.detect_schema()
        if schema_dst and schema_src and schema_src != schema_dst:
            if schema_src.difference(schema_dst) or schema_dst.difference(schema_src):
                raise SchemaSyncConflict(schema_src, schema_dst)

    if doc_sync is None:
        doc_sync = DocSync.ByKey()

    if selection is not None:  # The selection argument may be a jobs or job ids sequence.
        selection = {str(j) for j in selection}

    # Provide some information about this sync process.
    if selection:
        logger.info("Synchronizing selection ({}) of project '{}' to '{}'.".format(
            len(selection), source, destination))
    else:
        logger.info("Synchronizing project '{}' to '{}'.".format(source, destination))
    logger.more("'{}' -> '{}'".format(source.root_directory(), destination.root_directory()))
    if dry_run:
        logger.info("Performing dry run!")
    if exclude is not None:
        logger.more("File name exclude pattern: '{}'".format(exclude))
    logger.more("Sync strategy: '{}'".format(strategy))
    logger.more("Doc sync strategy: '{}'".format(doc_sync))

    # Sync the Project document.
    if not (doc_sync is DocSync.NO_SYNC or doc_sync == DocSync.COPY):
        with proxy.create_doc_backup(destination.document) as dst_proxy:
            doc_sync(source.document, dst_proxy)

    # Sync jobs from source to destination.
    logger.more("Collect all jobs to synchronize...")
    if selection is None:
        jobs_to_sync = list(source)
    else:
        jobs_to_sync = [job for job in source if job.get_id() in selection]

    N = len(jobs_to_sync)
    logger.more("Need to synchronize {} jobs.".format(N))
    count = ddict(int)

    def _clone_or_sync(src_job):
        try:
            destination.clone(src_job)
            logger.more("Cloned job '{}'.".format(src_job))
            return 1
        except DestinationExistsError as e:
            dst_job = destination.open_job(id=src_job.get_id())
            sync_jobs(src_job, dst_job, strategy, exclude, doc_sync, proxy)
            logger.more("Synchonized job '{}'.".format(src_job))
            return 2

    if parallel:
        num_processes = None if parallel is True else parallel
        logger.more("Parallelizing over {} threads for synchronization.".format(
            'multiple' if num_processes is None else num_processes))
        with ThreadPool(None if parallel is True else parallel) as pool:
            for i, ret in enumerate(pool.imap(_clone_or_sync, jobs_to_sync)):
                count[ret] += 1
                logger.info("Project sync progress: {}/{}".format(i+1, N))
    else:
        for i, src_job in enumerate(jobs_to_sync):
            count[_clone_or_sync(src_job)] += 1
            logger.info("Project sync progress: {}/{}".format(i+1, N))

    num_cloned, num_synchronized = count[1], count[2]
    logger.info("Cloned {} and synchronized {} job(s).".format(num_cloned, num_synchronized))

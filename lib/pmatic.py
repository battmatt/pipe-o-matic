"""Top level package for the Pipe-o-matic pipeline framework."""

# Author: Walker Hale (hale@bcm.edu), 2012
#         Human Genome Sequencing Center, Baylor College of Medicine
#
# This file is part of Pipe-o-matic.
#
# Pipe-o-matic is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pipe-o-matic is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Pipe-o-matic.  If not, see <http://www.gnu.org/licenses/>.

import abc
import collections
import contextlib
from datetime import datetime
import itertools
import os
import stat
import string
import subprocess
import sys
import uuid

import yaml


META_DIR_NAME = '.pmatic'
TRASH_DIR_NAME = '.trash_cans'
STAT_TESTS = [
    (getattr(stat, 'S_IS' + name), name)
    for name in 'BLK CHR DIR FIFO LNK REG SOCK'.split()
]
EVENT_TYPES = 'started finished failed reverted'.split()


def parse_args_and_env(args, parser):
    """Return results of parsing arguments and checking environment.
    Apply business rules to the arguments."""
    command = parser.parse_args(args)
    # TODO: add support for setting PMATIC_BASE in a config file.
    command.pmatic_base = os.environ['PMATIC_BASE']
    path = pipeline_path(command.pmatic_base, command.pipeline)
    if not os.path.isfile(path):
        parser.exit('%r is not a file' % path)
    if not os.path.isdir(command.context_path):
        parser.exit('%r is not a directory' % command.context_path)
    return command


def build_engine_from_namespace(namespace):
    """Construct a PipelineEngine and dispatch to user-function."""
    engine = PipelineEngine(namespace.pmatic_base, namespace.context_path,
                            namespace.verbose, namespace.params)
    return engine


class PipelineEngine(object):
    def __init__(self, pmatic_base, context_path, verbose=False, params=None):
        """command is a Namespace from parsing the command line.
        Typical values:
        pipeline_name='foo-1',
        pmatic_base='/...pipe-o-matic/test/pmatic_base',
        context_path='/.../pipe-o-matic/target/test/case01/execute',
        verbose=False,
        params=None
        """
        super(PipelineEngine, self).__init__()
        self.pmatic_base = abspath(pmatic_base)
        self.context_path = abspath(context_path)
        self.verbose = verbose
        self.params = params
        self.dependency_finder = DependencyFinder(pmatic_base)
        self.event_log = EventLog(self.context_path)
        self.pipeline_loader = PipelineLoader(
            pmatic_base, self.dependency_finder, self.event_log
        )

    def run(self, pipeline_name):
        """Main starting point. Will attempt to start or restart the
        pipeline."""
        self.debug('running %s in %s', pipeline_name, self.context_path)
        # TODO: Add command-line support for creating context directory.
        pipeline = self.pipeline_loader.load_pipeline(pipeline_name)
        self.event_log.ensure_log_exists()
        self.event_log.read_log()
        current_pipeline = self.event_log.get_current_pipeline_name()
        current_status = self.event_log.get_status()
        # TODO: Add support for restart-able (asynchronous) pipelines.
        if current_status not in ['never_run', 'finished']:
            fail('Cannot run, because pipeline %r has a status of %r',
                 (current_pipeline, current_status))
        dependencies = pipeline.get_dependencies()
        unlisted = set(dependency for dependency in dependencies
                  if not self.dependency_finder.check_listed(dependency))
        missing = set(dependency for dependency in (dependencies - unlisted)
                  if not self.dependency_finder.check_exists(dependency))
        bad_type = set(dependency for dependency
                   in (dependencies - unlisted - missing)
                   if not self.dependency_finder.check_type(dependency))
        if unlisted or missing or bad_type:
            fail_dependencies(
                self.dependency_finder, unlisted, missing, bad_type
            )
        namespace = Namespace()
        os.chdir(self.context_path)
        pipeline.run(namespace)

    def debug(self, message='', *args):
        """Format and print to stderr if verbose."""
        if self.verbose:
            print_err(message, *args)


class EventLog(object):
    """Manages recording a reading of pipeline events.
    Uses a lockfile to achieve atomicity."""
    # TODO: Start using lockfile.
    def __init__(self, context_path):
        super(EventLog, self).__init__()
        self.context_path = context_path
        self.events_path = os.path.join(meta_path(context_path), 'events')
        self.db_path = os.path.join(self.events_path, 'db')
        self.new_path = os.path.join(self.events_path, 'new')
        self.head_path = os.path.join(self.events_path, 'head')
        self.event_data = None

    def revert_one(self):
        """Assuming there has been at least one pipeline start, revert
        to the previous state."""
        self.read_log()
        pipeline_name = self.get_current_pipeline_name()
        assert pipeline_name
        event = None
        for index, event in enumerate(self.event_data):
            if event.what == 'started':
                break
        assert isinstance(event, Event)
        assert event.what == 'started'
        assert event.pipeline_name == pipeline_name
        new_head_id = event.parent_event_id
        restore_snapshot(event.snapshot, self.context_path)
        self.record_pipeline_reverted(pipeline_name, new_head_id)
        self.read_log()

    def record_pipeline_started(self, pipeline, **kwds):
        """Records start of a pipeline. Raises exception if another pipeline
        is already running or the last entry in the EventLog was an error."""
        self.ensure_log_exists()
        # TODO: Check for previous state.
        before_snapshot = create_snapshot(self.context_path)
        self.post_event(pipeline, 'started',
                        snapshot=before_snapshot, **kwds)

    def record_pipeline_finished(self, pipeline, **kwds):
        """Records completion of a pipeline. Raises exception unless the
        immediately previous log entry was "started"."""
        self.ensure_log_exists()
        # TODO: Check for previous state.
        self.post_event(pipeline, 'finished', **kwds)

    def record_pipeline_failed(self, pipeline, **kwds):
        """Records error of a pipeline. Raises exception unless the
        immediately previous log entry was "started"."""
        # TODO: Get some test coverage here.
        self.ensure_log_exists()
        # TODO: Check for previous state.
        self.post_event(pipeline, 'failed', **kwds)

    def record_pipeline_reverted(self, pipeline_name, new_head_id, **kwds):
        """Records reverting execution of a pipeline. Raises exception unless
        the immediately previous log entry was "failed" or "finished"."""
        self.ensure_log_exists()
        # TODO: Check for previous state.
        fake_pipeline = Namespace(pipeline_name=pipeline_name)
        self.post_event(fake_pipeline, 'reverted', **kwds)
        self.save_new_head(new_head_id)

    def get_status(self):
        """Return terse execution status. Possible values:
        never_run, started, finished, error."""
        if not self.log_exists:
            return 'never_run'
        if not self.event_data:
            self.read_log()
        if not self.event_data:
            return 'never_run'
        return self.event_data[0].what

    def get_current_pipeline_name(self):
        """Return name of currently executing pipeline or None."""
        if not self.log_exists:
            return None
        if not self.event_data:
            self.read_log()
        if not self.event_data:
            return None
        return self.event_data[0].pipeline_name

    def ensure_log_exists(self):
        """Create empty log inside self.meta_path if it is missing."""
        ensure_directory_exists(self.events_path, os.makedirs)
        ensure_directory_exists(self.db_path)
        ensure_directory_exists(self.new_path)

    def read_log(self):
        """Read or re-read log from disk"""
        self.event_data = None
        if not self.log_exists:
            return
        if not os.path.isfile(self.head_path):
            return
        event_data = []
        event_id = load_yaml_file(self.head_path)  # Read log head.
        while event_id:
            event = self.read_event(event_id)
            event_data.append(event)
            event_id = event.parent_event_id
        self.event_data = event_data

    def read_event(self, event_id):
        """Return specified event data"""
        event_data = load_yaml_file(
            os.path.join(self.db_path, event_id + '.yaml')
        )
        return Event(**event_data)

    def post_event(self, pipeline, what, **kwds):
        """Store the specified event, and update head."""
        if self.event_data:
            parent_event_id = self.event_data[0].id
        else:
            parent_event_id = None
            self.event_data = []
        event = Event(pipeline.pipeline_name, what, parent_event_id, **kwds)
        self.event_data.insert(0, event)
        self.save_event(event)
        self.save_new_head(event.id)

    def save_event(self, event):
        event_file_name = event.id + '.yaml'
        new_event_path = os.path.join(self.new_path, event_file_name)
        final_event_path = os.path.join(self.db_path, event_file_name)
        save_yaml_file(new_event_path, event.__dict__)
        os.rename(new_event_path, final_event_path)

    def save_new_head(self, event_id):
        new_head_path = os.path.join(self.new_path, 'head')
        save_yaml_file(new_head_path, event_id)
        os.rename(new_head_path, self.head_path)

    @property
    def log_exists(self):
        """Return True if there is a readable log."""
        return os.path.isdir(self.db_path)


class Event(object):
    """A single event in the event log"""
    def __init__(self, pipeline_name, what, parent_event_id, **kwds):
        assert what in EVENT_TYPES
        super(Event, self).__init__()
        self.file_type = 'event-1'
        self.pipeline_name = pipeline_name
        self.what = what
        self.parent_event_id = parent_event_id
        self.when = datetime.utcnow()
        if kwds:
            self.__dict__.update(kwds)
        if not hasattr(self, 'id'):  # TODO: doing this here maybe too eager
            self.id = gen_uuid_str()

    def __repr__(self):
        d = vars(self).copy()
        for n in 'pipeline_name what parent_event_id'.split():
            del d[n]
        return '%s(%r, %r, %r, **%r' % (
            self.__class__.__name__,
            self.pipeline_name,
            self.what,
            self.parent_event_id,
            d
        )


class DependencyFinder(object):
    """Keeps track of where the dependencies are located on disk."""
    def __init__(self, pmatic_base):
        super(DependencyFinder, self).__init__()
        self.pmatic_base = pmatic_base
        deployments_path = deployment_file_path(pmatic_base)
        deployment_data = load_yaml_file(deployments_path)
        file_type = deployment_data.pop('file_type')
        assert file_type == 'deployments-1', 'bad type of ' + deployments_path
        dependency_paths = {}
        for name, version_map in deployment_data.iteritems():
            for version, path in version_map.iteritems():
                dependency_paths[(name, version)] = self.construct_path(path)
        self.dependency_paths = dependency_paths

    def check_listed(self, dependency):
        """Verify that dependency is listed in deployments file."""
        name, version, dependency_type = dependency
        result = (name, version) in self.dependency_paths
        return result

    def check_exists(self, dependency):
        """Verify that dependency exists.
        Assumes dependency is listed in the deployments file."""
        path = self.path(dependency)
        result = os.path.exists(path)
        return result

    def check_type(self, dependency):
        """Verify that dependency has correct type.
        Assumes dependency is listed and exists."""
        path, dependency_type = self.path_and_type(dependency)
        test = {
            'directory': os.path.isdir,
            'file': os.path.isfile,
            'executable': is_executable,
            'link': os.path.islink,
        }[dependency_type]
        result = test(path)
        return result

    def path(self, dependency):
        """Return absolute path to dependency."""
        return self.path_and_type(dependency)[0]

    def path_and_type(self, dependency):
        """Return pair of (absolute path & type)."""
        name, version, dependency_type = dependency
        return self.dependency_paths[(name, version)], dependency_type

    def construct_path(self, path):
        """Return absolute value of path."""
        t = string.Template(path)
        result = abspath(t.substitute(dict(pmatic_base=self.pmatic_base)))
        return result


class PipelineLoader(object):
    """Maintains a registry of Pipeline classes and constructs pipelines from
    files."""
    def __init__(self, pmatic_base, dependency_finder, event_log):
        super(PipelineLoader, self).__init__()
        self.pmatic_base = pmatic_base
        self.dependency_finder = dependency_finder
        self.event_log = event_log

    def load_pipeline(self, pipeline_name):
        """Return pipeline object."""
        data = load_yaml_file(pipeline_path(self.pmatic_base, pipeline_name))
        try:
            meta_map = data[0]
        except KeyError:
            meta_map = data
        file_type = meta_map['file_type']
        pipeline_class_name, version = file_type.rsplit('-', 1)
        # TODO: Select class based on pipeline_class_name
        klass = SingleTaskPipeline
        pipeline = klass(self.dependency_finder, self.event_log,
                         pipeline_name, version, data)
        return pipeline


class AbstractPipeline(object):
    """Abstract base class for all pipeline classes.
    Uses Template Method Pattern."""
    __metaclass__ = abc.ABCMeta

    def __init__(self, dependency_finder, event_log,
                 pipeline_name, version, data):
        super(AbstractPipeline, self).__init__()
        self.dependency_finder = dependency_finder
        self.event_log = event_log
        self.pipeline_name = pipeline_name
        self.version = version
        self.load(data)

    @abc.abstractmethod
    def load(self, data):
        """Deserialize pipeline data."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_dependencies(self):
        """Recursively generate a set of all
        (dependency, version, dependency_type) triplets."""
        raise NotImplementedError

    def run(self, namespace):
        """Main entry point for a pipeline object."""
        self.implement_run(namespace)

    @abc.abstractmethod
    def implement_run(self, namespace):
        """Implementation hook."""
        raise NotImplementedError

    def record_pipeline_started(self, **kwds):
        self.event_log.record_pipeline_started(self, **kwds)

    def record_pipeline_failed(self, **kwds):
        self.event_log.record_pipeline_failed(self, **kwds)

    def record_pipeline_finished(self, **kwds):
        self.event_log.record_pipeline_finished(self, **kwds)


class SingleTaskPipeline(AbstractPipeline):
    """Pipelines that wrap just one executable."""
    def load(self, data):
        """Requirement of AbstractPipeline"""
        assert self.version == '1', (
            'SingleTaskPipeline currently only version 1'
        )
        self.executable = None
        self.arguments = []
        self.stdin = None
        self.stdout = None
        self.stderr = None
        # TODO: Ensure that stdout and stderr are always directed somewhere.
        self.__dict__.update(data)
        if not self.stdin:
            self.stdin = '/dev/null'
        assert self.executable
        assert self.version

    def get_dependencies(self):
        """Requirement of AbstractPipeline"""
        return set([(self.executable, self.version, 'executable')])

    def implement_run(self, namespace):
        """Requirement of AbstractPipeline"""
        executable_path = self.dependency_finder.path(
            self.get_dependencies().pop()
        )
        args = [executable_path]
        args.extend(self.arguments)
        self.record_pipeline_started()
        try:
            cfin = conditional_file(self.stdin)
            cfout = conditional_file(self.stdout, 'w')
            cferr = conditional_file(self.stderr, 'w')
            with cfin as stdin, cfout as stdout, cferr as stderr:
                proc = subprocess.Popen(
                    args, stdin=stdin, stdout=stdout, stderr=stderr
                )
                exit_code = proc.wait()
        except Exception, e:
            self.record_pipeline_failed(exception=str(e))
            raise
        else:
            if exit_code == 0:
                self.record_pipeline_finished()
            else:
                self.record_pipeline_failed(exit_code=exit_code)
                raise ExitCodeError(exit_code,
                                    'exit code from %r' % executable_path)


class ExitCodeError(EnvironmentError):
    """Signals that an external program returned a nonzero exit code."""
    pass


def restore_snapshot(snapshot_dict, context_path):
    """Restore the working directory to the state described in snapshot_dict
    using the contents of ./.pmatic/inode_snapshots to recover moved or
    deleted files."""
    assert isinstance(snapshot_dict, dict)
    current_scan = scan_directory(context_path)
    # Delete anything new.
    trash_can = TrashCan(context_path)
    items_to_check = list(sorted(current_scan.items()))
    for key, record in items_to_check:
        matching_record = snapshot_dict.get(key)  # None if not found
        if strip_permissions(record) != strip_permissions(matching_record):
            path = os.path.join(context_path, key)
            if os.path.lexists(path):
                trash_can.trash(key)
    # Restore anything old.
    for key, record in sorted(snapshot_dict.items()):
        format, mode, size, inode, symlink = record
        path = os.path.join(context_path, key)
        if not os.path.exists(path):
            if format == 'DIR':
                os.mkdir(path)
            elif format == 'LNK':
                os.symlink(symlink, path)
            else:
                target = os.path.join(meta_path(context_path),
                                      'inode_snapshots',
                                      str(inode))
                os.link(target, path)
        lchmod(path, mode)


def strip_permissions(record):
    if record == None:
        result = None
    else:
        format, mode, size, inode, symlink = record
        result = format, size, inode, symlink
    return result


def create_snapshot(context_path):
    """Prepare to restore the state of the working directory later: Make hard
    link "backups" of all but symlinks and directories. Make all regular files
    read-only. Return the dict returned by scan_directory."""
    result = scan_directory(context_path)
    inode_dir = os.path.join(meta_path(context_path), 'inode_snapshots')
    ensure_directory_exists(inode_dir, os.makedirs)
    for key, record in result.iteritems():
        path = os.path.join(context_path, key)
        assert os.path.exists(path)
        format, mode, size, inode, symlink = record
        if format not in ('DIR', 'LNK'):
            inode_file = os.path.join(inode_dir, str(inode))
            if os.path.exists(inode_file):
                if not os.path.samefile(path, inode_file):
                    os.remove(inode_file)
            if not os.path.exists(inode_file):
                os.link(path, inode_file)
        if format == 'REG':
            new_mode = mode & 07555  # TODO: may not be portable
            lchmod(path, new_mode)
    return result


def scan_directory(start_path, *exclude_paths):
    """Return dict path:(format, mode, size, inode, symlink).
    exclude_paths (default '.pmatic') will not be scanned."""
    if not exclude_paths:
        exclude_paths = (META_DIR_NAME, TRASH_DIR_NAME)
    result = {}
    for dir_path, dir_names, file_names in os.walk(start_path):
        remove_dir_names = []
        for dir_name in dir_names:
            key, format, mode, size, inode, symlink = stat_item(
                dir_name, dir_path, start_path
            )
            if key in exclude_paths:
                remove_dir_names.append(dir_name)
                continue
            result[key] = format, mode, size, inode, None
        for dir_name in remove_dir_names:
            dir_names.remove(dir_name)
        for file_name in file_names:
            key, format, mode, size, inode, symlink = stat_item(
                file_name, dir_path, start_path
            )
            result[key] = format, mode, size, inode, symlink
    return result


def stat_item(file_name, dir_path, base_path):
    path = os.path.join(dir_path, file_name)
    st = os.lstat(path)
    format_code = stat.S_IFMT(st.st_mode)
    format = decode_format(format_code)
    mode = stat.S_IMODE(st.st_mode)
    size = st.st_size if format in ('REG', 'LNK') else 0L
    inode = st.st_ino if format not in ('DIR', 'LNK') else 0L
    symlink = os.readlink(path) if format == 'LNK' else None
    key = os.path.relpath(path, base_path)
    return key, format, mode, size, inode, symlink


def decode_format(format_code):
    format = None
    for test_fcn, name in STAT_TESTS:
        if test_fcn(format_code):
            assert format is None, 'A file can only have one type.'
            format = name
    assert format, 'A filesystem object must have some type'
    return format


def lchmod(path, mode):
    """If path is not a symlink, do a regular chmod. If path is a symlink
    and os.lchmod is available, do os.lchmod. If path is a symlink and
    os.lchmod is not available, do nothing."""
    if os.path.islink(path):
        if hasattr(os, 'lchmod'):
            os.lchmod(path, mode)
    else:
        os.chmod(path, mode)


class TrashCan(object):
    """A place to move files, prior to deleting them."""
    def __init__(self, context_path):
        self.context_path = context_path
        self.trash_path = os.path.join(context_path, TRASH_DIR_NAME,
                                       datetime.utcnow().isoformat())

    def trash(self, rel_path):
        """Move the object at rel_path to the coresponding relative path
        inside self.trash_path. This method assumes that rel_path is inside
        self.context_path."""
        assert not os.path.isabs(rel_path)
        abs_path = os.path.join(self.context_path, rel_path)
        rel_dir_path = os.path.dirname(rel_path)
        dest_dir_path = os.path.join(self.trash_path, rel_dir_path)
        dest_path = os.path.join(self.trash_path, rel_path)
        ensure_directory_exists(dest_dir_path, os.makedirs)
        if os.path.isdir(abs_path) and os.path.exists(dest_path):
            os.rmdir(abs_path)
        else:
            os.rename(abs_path, dest_path)


def fail_dependencies(dependency_finder, unlisted, missing, bad_type):
    if unlisted:
        print_err('The following dependencies are not listed in %s:',
                  deployment_file_path(dependency_finder.pmatic_base))
        for dependency in sorted(unlisted):
            print_err('%r', dependency)
    if missing:
        print_err('The following dependencies are missing:')
        for dependency in sorted(missing):
            print_err('%r', dependency_finder.path(dependency))
    if bad_type:
        print_err('The following dependencies have the wrong type:')
        for dependency in sorted(bad_type):
            path, dependency_type = dependency_finder.path_and_type(dependency)
            print_err('need %r: %r', dependency_type, path)
    exit(1)


def fail(message, *args):
    """Format message to stderr and exit with a code of 1."""
    print_err(message, *args)
    exit(1)


def print_err(message, *args):
    """Format and print to stderr"""
    if len(args) == 1:
        args = args[0]
    message_str = message % args
    print >>sys.stderr, message_str


def exit(code):
    sys.exit(code)


def gen_uuid_str():
    """Returns a version 1 UUID. (See RFC 4122.)
    Nice hook for testing. For testing, you can replace this function
    by the bound next() method of some iterator. Example:
    pmatic.gen_uuid_str = iter([uuid1, uuid2, uuid3]).next"""
    return str(uuid.uuid1())


def ensure_directory_exists(dir_path, create_fcn=os.mkdir):
    """Create the specified directory if it is missing.
    create_fcn defaults to os.mkdir."""
    if not os.path.isdir(dir_path):
        create_fcn(dir_path)


def load_yaml_file(yaml_file_path):
    """Return YAML data in yaml_file_path."""
    with open(yaml_file_path) as fin:
        return yaml.load(fin)


def save_yaml_file(yaml_file_path, data):
    with open(yaml_file_path, 'w') as fout:
        # Use safe_dump to supress non-standard tags:
        yaml.safe_dump(data, fout, default_flow_style=False)


@contextlib.contextmanager
def conditional_file(file_path, mode='r', bufsize=-1):
    """Context manager for conditionally opening a file. Yield None if not
    file_path, otherwise yield the opened file and then close it at the end.
    """
    if not file_path:
        yield None
    else:
        file_object = open(file_path, mode, bufsize)
        yield file_object
        file_object.close()


def is_executable(path):
    """Return True if path is an executable. (Unix only)"""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def deployment_file_path(pmatic_base):
    return os.path.join(pmatic_base, 'deployments.yaml')


def pipeline_path(pmatic_base, pipeline_name):
    """Return the path to the specified pipeline."""
    return os.path.join(pmatic_base, 'pipelines', pipeline_name + '.yaml')


def meta_path(context_path):
    """Return the path to the .pmatic directory inside the context
    directory."""
    return os.path.join(context_path, META_DIR_NAME)


def abspath(path):
    """Convenience composition of os.path.abspath and os.path.expanduser"""
    return os.path.abspath(os.path.expanduser(path))


class Namespace(collections.Mapping):
    """Holds arbitrary attributes. Access either as an object or a mapping."""
    def __init__(self, *mappings, **kwds):
        super(Namespace, self).__init__()
        # Cannot use normal self.x=y, because we have __setattr__.
        mapping = ChainMap(*mappings, **kwds)
        super(Namespace, self).__setattr__('mapping', mapping)

    def __setattr__(self, name, value):
        self.mapping[name] = value

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.mapping)

    def __getattr__(self, name):
        return self.mapping[name]

    def __getitem__(self, key):
        return self.mapping[key]

    def __setitem__(self, key, value):
        self.mapping[key] = value

    def __delitem__(self, key):
        del self.mapping

    def __contains__(self, key):
        return key in self.mapping

    def __len__(self):
        return len(self.mapping)

    def __iter__(self):
        return iter(self.mapping)


class ChainMap(collections.Mapping):
    """Simple wrapper around a list of dicts. Last wins. Modifications apply
    to the possibly empty kwds mapping."""
    def __init__(self, *mappings, **kwds):
        super(ChainMap, self).__init__()
        self.mappings = list(mappings)
        self.mappings.append(kwds)

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.mappings)

    def __getitem__(self, key):
        for mapping in self.mappings[::-1]:
            if key in mapping:
                return mapping[key]
        raise KeyError(key)

    def __setitem__(self, key, value):
        self.mappings[-1][key] = value

    def __delitem__(self, key):
        del self.mappings[-1][key]

    def __contains__(self, key):
        for mapping in self.mappings:
            if key in mapping:
                return True
        return False

    def __iter__(self):
        return iter(set(itertools.chain(*self.mappings)))

    def __len__(self):
        return len(set(itertools.chain(*self.mappings)))

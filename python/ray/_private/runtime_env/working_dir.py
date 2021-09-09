from enum import Enum
from filelock import FileLock
import hashlib
import logging
import os
from pathlib import Path
import sys
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse
from zipfile import ZipFile

from ray.experimental.internal_kv import (_internal_kv_put, _internal_kv_get,
                                          _internal_kv_exists,
                                          _internal_kv_initialized)
from ray.job_config import JobConfig
from ray._private.thirdparty.pathspec import PathSpec
from ray._private.runtime_env import RuntimeEnvContext

# We need to setup this variable before using this module.
PKG_DIR = None

_logger = logging.getLogger(__name__)

FILE_SIZE_WARNING = 10 * 1024 * 1024  # 10MiB
# NOTE(edoakes): we should be able to support up to 512 MiB based on the GCS'
# limit, but for some reason that causes failures when downloading.
GCS_STORAGE_MAX_SIZE = 100 * 1024 * 1024  # 100MiB


class Protocol(Enum):
    """A enum for supported backend storage."""

    # For docstring
    def __new__(cls, value, doc=None):
        self = object.__new__(cls)
        self._value_ = value
        if doc is not None:
            self.__doc__ = doc
        return self

    GCS = "gcs", "For packages created and managed by the system."
    PIN_GCS = "pingcs", "For packages created and managed by the users."


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    if left and right:
        return bytes(a ^ b for (a, b) in zip(left, right))
    return left or right


def _dir_travel(
        path: Path,
        excludes: List[Callable],
        handler: Callable,
):
    e = _get_gitignore(path)
    if e is not None:
        excludes.append(e)
    skip = any(e(path) for e in excludes)
    if not skip:
        try:
            handler(path)
        except Exception as e:
            _logger.error(f"Issue with path: {path}")
            raise e
        if path.is_dir():
            for sub_path in path.iterdir():
                _dir_travel(sub_path, excludes, handler)
    if e is not None:
        excludes.pop()


def _zip_module(root: Path, relative_path: Path, excludes: Optional[Callable],
                zip_handler: ZipFile) -> None:
    """Go through all files and zip them into a zip file"""

    def handler(path: Path):
        # Pack this path if it's an empty directory or it's a file.
        if path.is_dir() and next(path.iterdir(),
                                  None) is None or path.is_file():
            file_size = path.stat().st_size
            if file_size >= FILE_SIZE_WARNING:
                _logger.warning(
                    f"File {path} is very large ({file_size} bytes). "
                    "Consider excluding this file from the working directory.")
            to_path = path.relative_to(relative_path)
            zip_handler.write(path, to_path)

    excludes = [] if excludes is None else [excludes]
    _dir_travel(root, excludes, handler)


def _hash_modules(
        root: Path,
        relative_path: Path,
        excludes: Optional[Callable],
) -> bytes:
    """Helper function to create hash of a directory.

    It'll go through all the files in the directory and xor
    hash(file_name, file_content) to create a hash value.
    """
    hash_val = None
    BUF_SIZE = 4096 * 1024

    def handler(path: Path):
        md5 = hashlib.md5()
        md5.update(str(path.relative_to(relative_path)).encode())
        if not path.is_dir():
            with path.open("rb") as f:
                data = f.read(BUF_SIZE)
                while len(data) != 0:
                    md5.update(data)
                    data = f.read(BUF_SIZE)
        nonlocal hash_val
        hash_val = _xor_bytes(hash_val, md5.digest())

    excludes = [] if excludes is None else [excludes]
    _dir_travel(root, excludes, handler)
    return hash_val


def _get_local_path(pkg_uri: str) -> str:
    assert PKG_DIR, "Please set PKG_DIR in the module first."
    (_, pkg_name) = _parse_uri(pkg_uri)
    return os.path.join(PKG_DIR, pkg_name)


def _parse_uri(pkg_uri: str) -> Tuple[Protocol, str]:
    uri = urlparse(pkg_uri)
    protocol = Protocol(uri.scheme)
    return (protocol, uri.netloc)


def _get_excludes(path: Path, excludes: List[str]) -> Callable:
    path = path.absolute()
    pathspec = PathSpec.from_lines("gitwildmatch", excludes)

    def match(p: Path):
        path_str = str(p.absolute().relative_to(path))
        path_str += "/"
        return pathspec.match_file(path_str)

    return match


def _get_gitignore(path: Path) -> Optional[Callable]:
    path = path.absolute()
    ignore_file = path / ".gitignore"
    if ignore_file.is_file():
        with ignore_file.open("r") as f:
            pathspec = PathSpec.from_lines("gitwildmatch", f.readlines())

        def match(p: Path):
            path_str = str(p.absolute().relative_to(path))
            if p.is_dir():
                path_str += "/"
            return pathspec.match_file(path_str)

        return match
    else:
        return None


# TODO(yic): Fix this later to handle big directories in better way
def get_project_package_name(working_dir: str, py_modules: List[str],
                             excludes: List[str]) -> str:
    """Get the name of the package by working dir and modules.

    This function will generate the name of the package by the working
    directory and modules. It'll go through all the files in working_dir
    and modules and hash the contents of these files to get the hash value
    of this package. The final package name is: _ray_pkg_<HASH_VAL>.zip
    Right now, only the modules given will be included. The dependencies
    are not included automatically.

    Examples:

    .. code-block:: python
        >>> import any_module
        >>> get_project_package_name("/working_dir", [any_module])
        .... _ray_pkg_af2734982a741.zip

 e.g., _ray_pkg_029f88d5ecc55e1e4d64fc6e388fd103.zip
    Args:
        working_dir (str): The working directory.
        py_modules (list[str]): The python module.
        excludes (list[str]): The dir or files that should be excluded

    Returns:
        Package name as a string.
    """
    RAY_PKG_PREFIX = "_ray_pkg_"
    hash_val = None
    if working_dir:
        if not isinstance(working_dir, str):
            raise TypeError("`working_dir` must be a string.")
        working_dir = Path(working_dir).absolute()
        if not working_dir.exists() or not working_dir.is_dir():
            raise ValueError(f"working_dir {working_dir} must be an existing"
                             " directory")
        hash_val = _xor_bytes(
            hash_val,
            _hash_modules(working_dir, working_dir,
                          _get_excludes(working_dir, excludes)))
    for py_module in py_modules or []:
        if not isinstance(py_module, str):
            raise TypeError("`py_module` must be a string.")
        module_dir = Path(py_module).absolute()
        if not module_dir.exists() or not module_dir.is_dir():
            raise ValueError(f"py_module {py_module} must be an existing"
                             " directory")
        hash_val = _xor_bytes(
            hash_val, _hash_modules(module_dir, module_dir.parent, None))
    return RAY_PKG_PREFIX + hash_val.hex() + ".zip" if hash_val else None


def create_project_package(working_dir: str, py_modules: List[str],
                           excludes: List[str], output_path: str) -> None:
    """Create a pckage that will be used by workers.

    This function is used to create a package file based on working directory
    and python local modules.

    Args:
        working_dir (str): The working directory.
        py_modules (list[str]): The list of path of python modules to be
            included.
        excludes (List(str)): The directories or file to be excluded.
        output_path (str): The path of file to be created.
    """
    pkg_file = Path(output_path).absolute()
    with ZipFile(pkg_file, "w") as zip_handler:
        if working_dir:
            # put all files in /path/working_dir into zip
            working_path = Path(working_dir).absolute()
            _zip_module(working_path, working_path,
                        _get_excludes(working_path, excludes), zip_handler)
        for py_module in py_modules or []:
            module_path = Path(py_module).absolute()
            _zip_module(module_path, module_path.parent, None, zip_handler)


def fetch_package(pkg_uri: str) -> int:
    """Fetch a package from a given uri if not exists locally.

    This function is used to fetch a pacakge from the given uri and unpack it.

    Args:
        pkg_uri (str): The uri of the package to download.

    Returns:
        The directory containing this package
    """
    pkg_file = Path(_get_local_path(pkg_uri))
    local_dir = pkg_file.with_suffix("")
    assert local_dir != pkg_file, "Invalid pkg_file!"
    if local_dir.exists():
        assert local_dir.is_dir(), f"{local_dir} is not a directory"
        return local_dir
    _logger.debug("Fetch packge")
    (protocol, pkg_name) = _parse_uri(pkg_uri)
    if protocol in (Protocol.GCS, Protocol.PIN_GCS):
        code = _internal_kv_get(pkg_uri)
        if code is None:
            raise IOError("Fetch uri failed")
        code = code or b""
        pkg_file.write_bytes(code)
    else:
        raise NotImplementedError(f"Protocol {protocol} is not supported")

    _logger.debug(f"Unpack {pkg_file} to {local_dir}")
    with ZipFile(str(pkg_file), "r") as zip_ref:
        zip_ref.extractall(local_dir)
    pkg_file.unlink()
    return local_dir


def _store_package_in_gcs(gcs_key: str, data: bytes) -> int:
    if len(data) >= GCS_STORAGE_MAX_SIZE:
        raise RuntimeError(
            "working_dir package exceeds the maximum size of 100MiB. You "
            "can exclude large files using the 'excludes' option to the "
            "runtime_env.")

    _internal_kv_put(gcs_key, data)
    return len(data)


def push_package(pkg_uri: str, pkg_path: str) -> int:
    """Push a package to uri.

    This function is to push a local file to remote uri. Right now, only GCS
    is supported.

    Args:
        pkg_uri (str): The uri of the package to upload to.
        pkg_path (str): Path of the local file.

    Returns:
        The number of bytes uploaded.
    """
    (protocol, pkg_name) = _parse_uri(pkg_uri)
    data = Path(pkg_path).read_bytes()
    if protocol in (Protocol.GCS, Protocol.PIN_GCS):
        return _store_package_in_gcs(pkg_uri, data)
    else:
        raise NotImplementedError(f"Protocol {protocol} is not supported")


def package_exists(pkg_uri: str) -> bool:
    """Check whether the package with given uri exists or not.

    Args:
        pkg_uri (str): The uri of the package

    Return:
        True for package existing and False for not.
    """
    assert _internal_kv_initialized()
    (protocol, pkg_name) = _parse_uri(pkg_uri)
    if protocol in (Protocol.GCS, Protocol.PIN_GCS):
        return _internal_kv_exists(pkg_uri)
    else:
        raise NotImplementedError(f"Protocol {protocol} is not supported")


def rewrite_runtime_env_uris(job_config: JobConfig) -> None:
    """Rewrite the uris field in job_config.

    This function is used to update the runtime field in job_config. The
    runtime field will be generated based on the hash of required files and
    modules.

    Args:
        job_config (JobConfig): The job config.
    """
    # For now, we only support local directory and packages
    uris = job_config.runtime_env.get("uris")
    if uris is not None:
        return
    working_dir = job_config.runtime_env.get("working_dir")
    py_modules = job_config.runtime_env.get("py_modules")
    excludes = job_config.runtime_env.get("excludes")
    if working_dir or py_modules:
        if excludes is None:
            excludes = []
        pkg_name = get_project_package_name(working_dir, py_modules, excludes)
        job_config.set_runtime_env_uris(
            [Protocol.GCS.value + "://" + pkg_name])


def upload_runtime_env_package_if_needed(job_config: JobConfig):
    """Upload runtime env if it's not there.

    It'll check whether the runtime environment exists in the cluster or not.
    If it doesn't exist, a package will be created based on the working
    directory and modules defined in job config. The package will be
    uploaded to the cluster after this.

    Args:
        job_config (JobConfig): The job config of driver.
    """
    pkg_uris = job_config.get_runtime_env_uris()
    if len(pkg_uris) == 0:
        return  # Return early to avoid internal kv check in this case.
    assert _internal_kv_initialized()
    for pkg_uri in pkg_uris:
        if not package_exists(pkg_uri):
            file_path = _get_local_path(pkg_uri)
            pkg_file = Path(file_path)
            working_dir = job_config.runtime_env.get("working_dir")
            py_modules = job_config.runtime_env.get("py_modules")
            excludes = job_config.runtime_env.get("excludes") or []
            _logger.info(f"{pkg_uri} doesn't exist. Create new package with"
                         f" {working_dir} and {py_modules}")
            if not pkg_file.exists():
                create_project_package(working_dir, py_modules, excludes,
                                       file_path)
            # Push the data to remote storage
            pkg_size = push_package(pkg_uri, pkg_file)
            _logger.info(f"{pkg_uri} has been pushed with {pkg_size} bytes")


def ensure_runtime_env_setup(pkg_uris: List[str]) -> Optional[str]:
    """Make sure all required packages are downloaded it local.

    Necessary packages required to run the job will be downloaded
    into local file system if it doesn't exist.

    Args:
        pkg_uri list(str): Package of the working dir for the runtime env.

    Return:
        Working directory is returned if the pkg_uris is not empty,
        otherwise, None is returned.
    """
    pkg_dir = None
    assert _internal_kv_initialized()
    for pkg_uri in pkg_uris:
        # For each node, the package will only be downloaded one time
        # Locking to avoid multiple process download concurrently
        pkg_file = Path(_get_local_path(pkg_uri))
        with FileLock(str(pkg_file) + ".lock"):
            pkg_dir = fetch_package(pkg_uri)
        sys.path.insert(0, str(pkg_dir))

    # Right now, multiple pkg_uris are not supported correctly.
    # We return the last one as working directory
    return str(pkg_dir) if pkg_dir else None


def setup_working_dir(runtime_env: dict,
                      context: RuntimeEnvContext,
                      logger: Optional[logging.Logger] = None):
    if not runtime_env.get("uris"):
        return

    # Overwrite the module-wide logger and PKG_DIR temporarily.
    # TODO(edoakes): we should be able to remove this by refactoring the
    # working_dir setup code into a class instead of using global vars.
    global _logger, PKG_DIR
    if logger:
        prev_logger = _logger
        _logger = logger

    assert context.resources_dir is not None
    prev_pkg_dir = PKG_DIR
    PKG_DIR = context.resources_dir

    working_dir = ensure_runtime_env_setup(runtime_env["uris"])
    context.command_prefix += [f"cd {working_dir}"]

    # Insert the working_dir as the first entry in PYTHONPATH. This is
    # compatible with users providing their own PYTHONPATH in env_vars.
    python_path = working_dir
    if "PYTHONPATH" in context.env_vars:
        python_path += os.pathsep + context.env_vars["PYTHONPATH"]
    context.env_vars["PYTHONPATH"] = python_path

    PKG_DIR = prev_pkg_dir

    if logger:
        _logger = prev_logger
import json as _json
import fnmatch as _fnmatch
import os as _os
import pathlib as _pathlib
import concurrent.futures as _futures

from . import _helpers

_MAX_WORKERS = 32
_IGNORE_DIRS = set()
_SCAN_HIDDEN_DIRS = True
_SCAN_HIDDEN_FILES = True

_WILDCARD_CHARS = set("*?[")

_DEEP_SCAN_CACHE: dict = {}


def _normalise(item: str) -> str:
    return item if any(c in item for c in _WILDCARD_CHARS) else f"*{item}*"


def _ignore_dir(path: str, name: str, ignore_dirs: set[str], scan_hidden: bool) -> bool:
    return (
        name in ignore_dirs or 
        path in ignore_dirs or (
            not scan_hidden and name.startswith('.')
        )
    )


def _should_consider_file(
    filename: str,
    *, 
    scan_hidden: bool, 
    search_file_names: set[str] | None,
    search_file_extensions: set[str] | None,
) -> bool:
    if not scan_hidden and filename.startswith("."):
        return False

    should_consider = True

    if search_file_names:
        should_consider = any(
            _fnmatch.fnmatchcase(filename.casefold(), _normalise(to_search).casefold())
            for to_search in search_file_names
        )

        if not should_consider:
            return False

    if search_file_extensions:
        should_consider = False
        for ext in search_file_extensions:
            ext_lower = ext.lower()
            filename_lower = filename.lower()

            if ext_lower.startswith("."):
                ext_lower = ext_lower.split(".")[1]

            if filename_lower.endswith("." + ext_lower):
                should_consider = True
                break

    return should_consider


class _TaskManager:
    def __init__(self, params: dict) -> None:
        self._max_workers: int = 0

        self._path: str = params["path"]
        self._ignore_dirs: set[str] = params["ignore_dirs"]
        self._scan_hidden_dirs: bool = params["scan_hidden_dirs"]
        self._scan_hidden_files: bool = params["scan_hidden_files"]
        self._search_file_names: set[str] | None = params["search_file_names"]
        self._search_file_extensions: set[str] | None = params["search_file_extensions"]
    
    def skim_dir(self, path: str) -> dict:
        result: dict = {
            "__path__": str(path),
            "__files__": [],
        }

        try:
            with _os.scandir(path) as it:
                for entry in it:
                    if (
                        entry.is_file(follow_symlinks=False)
                        and _should_consider_file(
                            entry.name,
                            scan_hidden=self._scan_hidden_files, 
                            search_file_names=self._search_file_names,
                            search_file_extensions=self._search_file_extensions
                        )
                    ):
                        result["__files__"].append(entry.name)
                    
                    elif (
                        entry.is_dir(follow_symlinks=False)
                        and not _ignore_dir(entry.path, entry.name, self._ignore_dirs, self._scan_hidden_dirs)
                    ):
                        result[entry.name] = {
                            "__path__": str(entry.path),
                            "__files__": []
                        }
        
        except OSError as e:
            result["__error__"] = str(e)
            
        return result
    
    def _crawl_dir(self, out_bucket: dict) -> None:
        assert "__path__" in out_bucket, "Provided bucket has no '__path__'"
        assert "__files__" in out_bucket, "Provided bucket has no '__files__'"

        crawl_targets = [(out_bucket["__path__"], out_bucket)]
        while crawl_targets:
            target_path, target_bucket = crawl_targets.pop(0)

            try:
                with _os.scandir(target_path) as it:
                    for entry in it:
                        if (
                            entry.is_file(follow_symlinks=False)
                            and _should_consider_file(
                                entry.name,
                                scan_hidden=self._scan_hidden_files, 
                                search_file_names=self._search_file_names,
                                search_file_extensions=self._search_file_extensions
                            )
                        ):
                            target_bucket["__files__"].append(entry.name)
                        
                        elif (
                            entry.is_dir(follow_symlinks=False)
                            and not _ignore_dir(entry.path, entry.name, self._ignore_dirs, self._scan_hidden_dirs)
                        ):
                            target_bucket[entry.name] = {
                                "__path__": entry.path,
                                "__files__": []
                            }
                            crawl_targets.append(
                                (entry.path, target_bucket[entry.name])
                            )

            except OSError as e:
                target_bucket["__error__"] = str(e)
        
        print("Crawl finished", out_bucket["__path__"])
    
    @property
    def workers_deployed(self) -> int:
        return self._max_workers
    
    def begin_scan(self) -> dict:
        result_bucket: dict = self.skim_dir(self._path)

        if "__error__" in result_bucket:
            return result_bucket

        root_width = 0
        for _, value in result_bucket.items():
            if isinstance(value, dict):
                root_width += 1

        self._max_workers = min(root_width, _MAX_WORKERS)

        with _futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = [
                executor.submit(self._crawl_dir, value)
                for _, value in result_bucket.items()
                if isinstance(value, dict)
            ]
        
            _futures.wait(futures, return_when=_futures.FIRST_EXCEPTION)

            for f in futures:
                if exc := f.exception():
                    raise exc

        return result_bucket


class Scanner:
    def __init__(self, directory: str, config: dict) -> None:
        if not directory.startswith("~"):
            if not directory.startswith("/"):
                directory = "~/" + directory

        self._root_path = _pathlib.Path(directory).expanduser()
        self._scan_result: dict[str, str | list[str] | dict] = {}

        self._gen_summary: bool = config.get("summarize", False)
        self._enable_cache: bool = config.get("enable_cache", True)
        self._ignore_dirs: set[str] = config.get("ignore_dirs", _IGNORE_DIRS)
        self._output_file_name: str | None = config.get("output_file_name", None)
        self._scan_hidden_dirs: bool = config.get("scan_hidden_dirs", _SCAN_HIDDEN_DIRS)
        self._scan_hidden_files: bool = config.get("scan_hidden_files", _SCAN_HIDDEN_FILES)
        self._search_file_names: set[str] | None = config.get("search_file_names", None)
        self._search_file_extensions: set[str] | None = config.get("search_file_extensions", None)

        self._task_man = _TaskManager(
            params={
                "path": str(self._root_path),
                "ignore_dirs": self._ignore_dirs,
                "scan_hidden_dirs": self._scan_hidden_dirs,
                "scan_hidden_files": self._scan_hidden_files,
                "search_file_names": self._search_file_names,
                "search_file_extensions": self._search_file_extensions
            }   
        )
    
    @property
    def result(self) -> dict[str, str | list[str] | dict]:
        return self._scan_result
    
    @property
    def summary(self) -> dict[str, int]:
        error_count, dir_count, file_count = self._summarize()

        return {
            "dir_count": dir_count,
            "file_count": file_count,
            "error_count": error_count,
        }
    
    @property
    def workers_deployed(self) -> int:
        return self._task_man.workers_deployed

    @_helpers.time_it()
    def _deep_scan_dir(self) -> None:
        if not (self._root_path.exists() and self._root_path.is_dir()):
            self._scan_result = {
                "__path__": str(self._root_path),
                "__files__": [],
                "__error__": f"Provided path '{self._root_path}' does not exist."
            }
            return

        self._scan_result = self._task_man.begin_scan()

    def _summarize(self, bucket: dict | None = None) -> tuple[int, int, int]:
        if bucket is None:
            bucket = self._scan_result

        if "__error__" in  bucket:
            return 1, 0, 0

        error_count = 0
        dir_count = len(bucket) - 2
        file_count = len(bucket["__files__"])

        for _, value in bucket.items():
            if isinstance(value, dict):
                ret = self._summarize(bucket=value)
                dir_count += ret[1]
                file_count += ret[2]
                error_count += ret[0]

        return error_count, dir_count, file_count
    
    def shallow_scan(self) -> dict[str, str | list[str]]:
        print("⏳ Shallow scan", str(self._root_path), flush=True)
        scan_result = self._task_man.skim_dir(str(self._root_path))

        result: dict = {
            "path": "",
            "dirs": [],
            "files": [],
        }
        for key in scan_result:
            if key == "__path__":
                result["path"] = scan_result[key]

            elif key == "__files__":
                result["files"] = scan_result[key]
            
            elif key == "__error__":
                result["__error__"] = scan_result[key]

            elif isinstance(scan_result[key], dict):
                result["dirs"].append(key)
        
        print(_json.dumps(result, indent=2), flush=True)

        return result
    
    def deep_scan(self):
        print("⏳ Deep scan", str(self._root_path), flush=True)
        self._deep_scan_dir()

        if self._output_file_name:
            _os.makedirs("outputs", exist_ok=True)
            out_file_path = f"outputs/{self._output_file_name}.json"
            print(f"✍️   Writing '{out_file_path}' ... ", end="", flush=True)
            with open(out_file_path, "w") as fh:
                _json.dump(self._scan_result, fh, indent=4)
            print("✅")

        if self._gen_summary:
            print("🔍   Summarizing... ", end="", flush=True)
            errors, dirs_count, files_count = self._summarize()
            print("✅")

            print("")
            print("Scanned", str(self._root_path))
            print(" - Hidden dirs:", "✅" if self._scan_hidden_dirs else "❌")
            print(" - Hidden files:", "✅" if self._scan_hidden_files else "❌")
            print(" - Ignored dirs:", self._ignore_dirs or "None")
            print(" - File names:", self._search_file_names or "All")
            print(" - File extensions:", self._search_file_extensions or "All")
            print("")
            print(f"- Workers: {self.workers_deployed}")
            print(f"- Total dirs: {dirs_count:,}")
            print(f"- Total files: {files_count:,}")
            print(f"- Failed scans: {errors:,}")
        
        print("✅ Deep scan complete.", flush=True)
    
    def search_scan(self) -> dict[str, list[str]]:
        print("⏳ Search scan", str(self._root_path), flush=True)

        self.deep_scan()
        search_result: dict[str, list[str]] = {}

        def _compile_result(bucket: dict | None = None):
            if bucket is None:
                bucket = self._scan_result
            
            if "__error__" in bucket:
                return None
            
            if bucket["__files__"]:
                search_result[bucket["__path__"]] = bucket["__files__"]

            for _, value in bucket.items():
                if isinstance(value, dict):
                    _compile_result(value)
        
        _compile_result()
        return search_result

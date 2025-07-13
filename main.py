import lib as _lib


def main():
    ignore_dirs = set([
        ".ssh",
        ".git",
        "cache",
        ".venv",
        "__pycache__",
        "credintials",
        "_credintials",
        "legacy_credentials",
    ])

    scanner = _lib.Scanner(
        directory="/",
        config={
            "summarize": True,
            "ignore_dirs": ignore_dirs,
            "scan_hidden_dirs": True,
            "scan_hidden_files": True,
            "output_file_name": "files",
            "search_file_names": set([
                "scan",
            ]),
            "search_file_extensions": set([
                "pdf",
            ]),
        },
    )
    scanner.deep_scan()


if __name__ == "__main__":
    main()

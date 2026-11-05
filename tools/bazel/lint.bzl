"""Runs ruff as a Bazel test."""

load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_python//python:defs.bzl", "py_test")

_RUFF_RUNNER = [
    "import os, subprocess, sys",
    "import ruff",
    "# The wheel ships its binary at <wheel>/bin/ruff, a sibling of site-packages.",
    "_site_packages = os.path.dirname(os.path.dirname(ruff.__file__))",
    "_ruff_bin = os.path.join(os.path.dirname(_site_packages), 'bin', 'ruff')",
    "os.chdir(os.path.dirname(os.path.abspath(__file__)))",
    "sys.exit(subprocess.call([_ruff_bin, *sys.argv[1:]]))",
    "",
]

def py_ruff_test(name, srcs, config, ruff_files, ruff_args, deps = [], size = "small", **kwargs):
    """Run ruff over explicit paths, never ".": that would walk the runfiles venv.

    Args:
      name: target name.
      srcs: the .py files ruff should see.
      config: pyproject.toml carrying [tool.ruff].
      ruff_files: the hub's extracted wheel filegroup holding bin/ruff.
      ruff_args: ruff subcommand, flags and paths.
      deps: must include requirement("ruff").
      size: Bazel test size.
      **kwargs: forwarded to py_test.
    """
    main = "_%s_runner.py" % name
    write_file(
        name = "_gen_%s_runner" % name,
        out = main,
        content = _RUFF_RUNNER,
        testonly = True,
    )
    py_test(
        name = name,
        srcs = [main] + srcs,
        main = main,
        args = ruff_args,
        data = [config, ruff_files],
        deps = deps,
        size = size,
        **kwargs
    )

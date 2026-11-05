"""Runs listed test files through pytest under the package's pytest config."""

load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_python//python:defs.bzl", "py_test")

def py_pytest_test(name, tests, config, deps = [], data = [], env = {}, size = "small", **kwargs):
    """Define a py_test that runs `tests` through pytest.

    Args:
      name: target name.
      tests: test source files to collect.
      config: pyproject.toml carrying [tool.pytest.ini_options].
      deps: py_library and requirement() deps; must include pytest.
      data: runtime data files.
      env: test environment variables.
      size: Bazel test size.
      **kwargs: forwarded to py_test.
    """
    main = "_pytest_main_%s.py" % name
    write_file(
        name = "_gen_%s_main" % name,
        out = main,
        content = [
            "import sys",
            "import pytest",
            "",
            "if __name__ == '__main__':",
            "    sys.exit(pytest.main(sys.argv[1:]))",
            "",
        ],
        testonly = True,
    )
    py_test(
        name = name,
        srcs = [main] + tests,
        main = main,
        args = [
            "-ra",
            "-p",
            "no:cacheprovider",
            "-c",
            "$(rootpath %s)" % config,
        ] + ["$(rootpath %s)" % t for t in tests],
        data = data + [config],
        deps = deps,
        env = env,
        size = size,
        **kwargs
    )

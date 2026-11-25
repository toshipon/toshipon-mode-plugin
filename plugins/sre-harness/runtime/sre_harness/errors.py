"""Exit codes are part of the contract: callers branch on them."""

EXIT_OK = 0
EXIT_QUALITY = 1  # a quality gate failed, or the result is incomplete
EXIT_INPUT = 2  # the input or the configuration is wrong
EXIT_BOUNDARY = 3  # refused by a safety boundary


class HarnessError(Exception):
    exit_code = EXIT_QUALITY


class QualityGateError(HarnessError):
    exit_code = EXIT_QUALITY


class InputError(HarnessError):
    exit_code = EXIT_INPUT


class BoundaryError(HarnessError):
    exit_code = EXIT_BOUNDARY

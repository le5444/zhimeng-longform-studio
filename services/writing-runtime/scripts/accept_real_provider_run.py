"""Run one credential-safe real Provider acceptance through Writing Runtime."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from zhimeng_runtime.provider_probe import (  # noqa: E402
    ProviderProbeError,
    SecretLeakError,
    read_api_key_from_environment,
)
class SafeArgumentParser(argparse.ArgumentParser):
    """Do not echo an accidentally supplied credential in parser errors."""

    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: invalid arguments\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = SafeArgumentParser(
        allow_abbrev=False,
        description=(
            "Run a real OpenAI-compatible Provider through the managed Writing "
            "Runtime and write a metadata-only acceptance report."
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--accept-reported-model",
        action="append",
        default=[],
        help=(
            "Explicit accepted SSE model alias. Repeat for each allowed alias; "
            "exact requested-model equality needs no alias."
        ),
    )
    parser.add_argument(
        "--api-key-env",
        required=True,
        help="Name of the environment variable containing the API key.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON path under the workspace .runtime directory.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--provider-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--allow-insecure-loopback-http",
        action="store_true",
        help="Allow cleartext HTTP only for localhost or a loopback IP.",
    )
    parser.add_argument(
        "--writer-mode",
        choices=("repair_only", "author_review_change_proposal"),
        default="repair_only",
        help=(
            "repair_only asks the Provider for a repair plan only. "
            "author_review_change_proposal asks it to read the chapter and "
            "propose reviewable prose as a same-Run ChangeSet; the proposal "
            "still requires author approval before any project write."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of an existing report path.",
    )
    return parser.parse_args(argv)


def _report_path(requested: Path | None) -> Path:
    runtime_root = (WORKSPACE_ROOT / ".runtime").resolve()
    if requested is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        requested = (
            runtime_root
            / "provider-acceptance"
            / "reports"
            / f"runtime-provider-{timestamp}-{uuid.uuid4().hex[:8]}.json"
        )
    elif not requested.is_absolute():
        requested = WORKSPACE_ROOT / requested
    target = requested.expanduser().resolve()
    try:
        target.relative_to(runtime_root)
    except ValueError as exc:
        raise ProviderProbeError(
            "acceptance report must stay under the workspace .runtime directory"
        ) from exc
    if target.suffix.casefold() != ".json":
        raise ProviderProbeError("acceptance report must use a .json suffix")
    return target


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_args(argv)
        api_key = read_api_key_from_environment(arguments.api_key_env)
        output = _report_path(arguments.output)
        if api_key in str(output):
            raise SecretLeakError("acceptance output path contains credential material")
        from runtime_provider_acceptance import (
            RuntimeProviderAcceptanceConfig,
            run_runtime_provider_acceptance,
            write_runtime_provider_acceptance_report,
        )

        report = run_runtime_provider_acceptance(
            RuntimeProviderAcceptanceConfig(
                workspace_root=WORKSPACE_ROOT,
                base_url=arguments.base_url,
                model=arguments.model,
                api_key_env=arguments.api_key_env,
                api_key=api_key,
                accepted_reported_models=tuple(arguments.accept_reported_model),
                timeout_seconds=arguments.timeout_seconds,
                provider_timeout_seconds=arguments.provider_timeout_seconds,
                max_tokens=arguments.max_tokens,
                temperature=arguments.temperature,
                allow_insecure_loopback_http=(
                    arguments.allow_insecure_loopback_http
                ),
                writer_mode=arguments.writer_mode,
            )
        )
        write_runtime_provider_acceptance_report(
            output,
            report,
            api_key=api_key,
            overwrite=arguments.overwrite,
        )
    except SecretLeakError:
        print("Provider acceptance refused secret-bearing material.", file=sys.stderr)
        return 70
    except ProviderProbeError:
        print("Provider acceptance configuration or output error.", file=sys.stderr)
        return 64
    except Exception:
        print("Provider acceptance failed before a safe report was written.", file=sys.stderr)
        return 1

    print(f"Runtime Provider acceptance report written: {output}")
    return 0 if report.acceptance_status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

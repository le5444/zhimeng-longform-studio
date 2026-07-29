"""Run a credential-safe OpenAI-compatible Provider capability probe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.provider_probe import (  # noqa: E402
    CompatibleProviderProbe,
    DEFAULT_USER_AGENT,
    ProviderProbeError,
    SecretLeakError,
    read_api_key_from_environment,
    report_has_measured_failures,
    write_provider_probe_report,
)


class SafeArgumentParser(argparse.ArgumentParser):
    """Avoid echoing a mistakenly supplied secret in argparse error output."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: invalid arguments\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = SafeArgumentParser(
        description=(
            "Probe a single OpenAI-compatible Provider/model without storing or "
            "printing its API key. Use a caller-chosen .runtime output path."
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--api-key-env",
        required=True,
        help="Name of the environment variable containing the API key.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Required report path; recommended under .runtime/provider-probes/.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="HTTP User-Agent; override only when a Provider documents a different requirement.",
    )
    parser.add_argument(
        "--allow-insecure-loopback-http",
        action="store_true",
        help="Allow cleartext HTTP only for localhost or a loopback IP.",
    )
    parser.add_argument(
        "--skip-streaming",
        action="store_true",
        help="Record streaming as not_measured rather than sending a stream request.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of the caller-chosen report file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_args(argv)
        api_key = read_api_key_from_environment(arguments.api_key_env)
        report = CompatibleProviderProbe(
            base_url=arguments.base_url,
            model=arguments.model,
            api_key_env=arguments.api_key_env,
            api_key=api_key,
            user_agent=arguments.user_agent,
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
            timeout_seconds=arguments.timeout_seconds,
        ).run(include_streaming=not arguments.skip_streaming)
        write_provider_probe_report(
            arguments.output,
            report,
            api_key=api_key,
            overwrite=arguments.overwrite,
        )
    except SecretLeakError:
        print("Provider probe refused to write secret material.", file=sys.stderr)
        return 70
    except ProviderProbeError:
        print("Provider probe configuration or output error.", file=sys.stderr)
        return 64
    except Exception:
        # Do not render exception text: an upstream library may include request data.
        print("Provider probe failed before a safe report was written.", file=sys.stderr)
        return 1

    print("Provider capability report written.")
    return 2 if report_has_measured_failures(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

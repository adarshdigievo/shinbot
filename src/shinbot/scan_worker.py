"""Run unmodified upstream analyzers in a separate, credential-free process."""

import json
import resource
import sys
from importlib.metadata import version


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8_000_000, 8_000_000))
    from skill_scanner import SkillScanner
    scanner = SkillScanner()
    try:
        result = scanner.scan_skill(sys.argv[1], lenient=True).to_dict()
        print(json.dumps({"version": version("cisco-ai-skill-scanner"), "result": result}))
    finally:
        scanner.close()


if __name__ == "__main__":
    main()

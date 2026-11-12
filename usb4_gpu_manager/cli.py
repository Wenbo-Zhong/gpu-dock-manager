import argparse
import json
import os
import sys

from .client import Client
from .displays import INTERNAL_DISPLAY_ACTIONS
from .internal_gpu import ACTIONS as INTERNAL_ACTIONS, internal_switch_blocker, internal_switch_needed


def main():
    parser = argparse.ArgumentParser(description="USB4 GPU Manager local client")
    internal_actions = INTERNAL_ACTIONS | INTERNAL_DISPLAY_ACTIONS | {"internal-status"}
    parser.add_argument("action", choices=["status", "logs", "attach", "detach", "queue-detach", "cancel-detach", "rollback", "recover", "disable-displays", "restore-displays", *sorted(internal_actions)])
    parser.add_argument("--pci", help="PCI address for an internal GPU action")
    args = parser.parse_args()
    client = Client()
    try:
        if args.action == "recover":
            if os.geteuid() != 0:
                raise PermissionError("离线恢复需要 sudo usb4-gpu-managerctl recover")
            from .deployment import rollback
            result = rollback()
        elif args.action == "status":
            result = client.status()
        elif args.action == "logs":
            result = client.call("GetLogs")
        else:
            options = {}
            if args.action in internal_actions:
                if not args.pci:
                    raise RuntimeError("请用 --pci 指定内置 4070 的 PCI 地址。")
                options["expected_pci"] = args.pci
            if args.action == "internal-status":
                print(json.dumps(client.internal_gpu_status(args.pci), ensure_ascii=False, indent=2))
                return
            if args.action in INTERNAL_ACTIONS:
                current = client.internal_gpu_status(args.pci)
                print(json.dumps(current, ensure_ascii=False, indent=2), file=sys.stderr)
                blocker = internal_switch_blocker(current, args.action)
                if blocker:
                    print(json.dumps(blocker, ensure_ascii=False))
                    if internal_switch_needed(current, args.action):
                        raise SystemExit(1)
                    return
            if args.action in {"detach", "queue-detach", "disable-displays"}:
                target = client.status().get("target")
                if not target:
                    raise RuntimeError("没有唯一目标显卡。")
                options["expected_pci"] = target["pci"]
            result = client.perform(args.action, options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        parser.exit(1, str(error) + "\n")

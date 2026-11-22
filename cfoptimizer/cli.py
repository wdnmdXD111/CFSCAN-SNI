from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from .config import load_config, parse_regions, validate
from .lock import RunLock
from .pipeline import run
from .publish import publish
from .util import OptimizerError, resolve_path

ROOT = Path(__file__).resolve().parent.parent


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="CFSCAN-SNI：端口 → Lite → 地区 → TCP 前 N → Zone/可选反代出口过滤 → 可选测速/推送"
    )
    result.add_argument("command", choices=["run", "init", "check", "sync"], nargs="?", default="run")
    result.add_argument(
        "--config", default=str(ROOT / "config.json"), help="JSON 配置路径，其他相对路径基于此文件所在目录"
    )
    result.add_argument("--regions", help="覆盖地区配额，例如 HK:3,JP:3,SG:3；所有地区用 '*:2'")
    result.add_argument(
        "--region-basis", choices=["ipinfo", "colo"], help="IP 所属国家或实测 CF-Ray 机房国家"
    )
    result.add_argument("--no-github", action="store_true", help="本次不推送 GitHub")
    result.add_argument("--debug", action="store_true", help="显示异常调用栈")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )
    path = Path(args.config).resolve()
    try:
        if args.command == "init":
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise OptimizerError(f"{path} 已存在，不覆盖")
            shutil.copyfile(ROOT / "config.example.json", path)
            print(f"已创建 {path}，请填写 zone.domain 和 IPinfo Lite Token 后运行。")
            return 0
        cfg = load_config(path)
        if args.regions:
            cfg["regions"] = parse_regions(args.regions)
        if args.region_basis:
            cfg["region_basis"] = args.region_basis
        if args.regions or args.region_basis:
            cfg = validate(cfg)
        if args.command == "check":
            print(
                json.dumps(
                    {
                        "config": str(path),
                        "region_basis": cfg["region_basis"],
                        "regional_mode": cfg["regional_mode"],
                        "top_n": cfg["top_n"],
                        "regions": cfg["regions"],
                        "zone": cfg["zone"]["domain"],
                        "candidates": cfg["candidates"],
                        "tcp": cfg["tcp"],
                        "speed_enabled": cfg["speed"]["enabled"],
                        "proxyip_enabled": cfg["proxyip"]["enabled"],
                        "ip_risk_enabled": cfg["proxyip"]["enabled"] and cfg["proxyip"]["risk"]["enabled"],
                        "zone_cache": {
                            key: cfg["zone"][key]
                            for key in (
                                "cache_enabled",
                                "cache_file",
                                "cache_ttl_hours",
                                "cache_failure_ttl_hours",
                            )
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print("配置结构校验通过；本命令不做网络探测。")
            return 0
        with RunLock(path.parent / ".state" / "run.lock"):
            if args.command == "sync":
                if args.no_github or not cfg["github"]["enabled"]:
                    raise OptimizerError("sync 要求 github.enabled=true，且不能同时指定 --no-github")
                result = publish(resolve_path(path.parent, cfg["output"]["file"]), cfg["github"], path.parent)
                return 0 if result.success else 2
            return asyncio.run(run(cfg, path.parent, publish_result=not args.no_github))
    except KeyboardInterrupt:
        logging.warning("用户中断；未完成的任务不会被记为成功")
        return 130
    except (OptimizerError, OSError) as exc:
        logging.error("%s", exc)
        if args.debug:
            logging.exception("详细异常")
        return 1
    except Exception:
        if args.debug:
            logging.exception("运行失败")
        else:
            logging.error("运行异常，可加 --debug 查看详情；本次不算成功")
        return 1

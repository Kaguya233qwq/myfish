import argparse
import asyncio
import os
import runpy
import subprocess
import sys
from pathlib import Path

from loguru import logger


def init_project(project_name: str):
    """初始化一个新的工作区目录"""
    target_dir = Path.cwd() / project_name

    if target_dir.exists():
        logger.error(f"❌ 目录 {project_name} 已存在，请换一个项目名！")
        sys.exit(1)

    try:
        target_dir.mkdir()
        (target_dir / "plugins").mkdir()
        (target_dir / "data").mkdir()
        from myfish.core.config import ConfigManager

        ConfigManager._generate_default_config(target_dir / "config.toml")
        (target_dir / ".gitignore").write_text(
            "data/\n.myfish/\n.venv/\n__pycache__/\nconfig.toml\n*.toml.bak.*\n*.toml.schema.json\n.config-*\n",
            encoding="utf-8",
        )

        readme_text = (
            f"# {project_name}\n\n"
            "这是一个基于 Myfish 框架的机器人工作区。\n\n"
            "- 🔌 **插件开发**: 请在`plugins/` 目录下新建单py文件或python的pkg, 框架启动时会自动导入。\n"
            "- 💾 **数据存储**: 账号凭证及其他持久化数据会自动存入 `data/` 目录。\n\n"
            "**🚀 一键启动:**\n"
            "在当前目录下先执行 `myfish sync`，再执行 `myfish run`\n"
        )
        (target_dir / "README.md").write_text(readme_text, encoding="utf-8")

        logger.success(f"🎉 初始化 Myfish 工作区成功: {project_name}")
        logger.info(
            f"👉 接下来请执行:\n    cd {project_name}\n    myfish sync\n    myfish run"
        )

    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ 初始化失败: {e}")


def run_bot():
    """启动机器人引擎
    优先寻找当前目录下的 bot.py 作为入口，否则使用默认的 myfish.main入口
    """
    from myfish.core.config import load_config
    from myfish.core.dependency import (
        DependencyError,
        PluginDependencies,
        runtime_paths,
    )

    environment, python = runtime_paths(Path.cwd().resolve())
    if Path(sys.prefix).absolute() != environment.absolute():
        if not python.is_file():
            raise DependencyError("运行环境尚未建立，请先执行 myfish sync")
        try:
            code = subprocess.call(
                [
                    str(python),
                    "-m",
                    "myfish.cli",
                    "run",
                ]
            )
        except KeyboardInterrupt:
            logger.info("myfish cli已手动退出。")
            return
        raise SystemExit(code)

    config = load_config()
    PluginDependencies(config).check()

    cwd = Path.cwd()
    bot_file = cwd / "bot.py"

    if str(cwd) not in sys.path:
        sys.path.insert(0, str(cwd))

    try:
        if bot_file.exists():
            logger.info("🔧 使用自定义的入口文件 bot.py 启动...")
            runpy.run_path(str(bot_file), run_name="__main__")
        else:
            from myfish.main import main as start_bot

            logger.info("🐟 启用 Myfish默认的入口启动...")
            asyncio.run(start_bot(config))

    except KeyboardInterrupt:
        logger.info("bot已手动停止。")
    except Exception as e:
        logger.exception(f"❌ 运行过程中发生致命错误: {e}")
        raise SystemExit(1) from e


def main():
    """CLI 主路由"""
    parser = argparse.ArgumentParser(
        prog="myfish", description="🐟 Myfish: 现代化的跨平台Bot开发框架"
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="可用命令")

    # init
    init_parser = subparsers.add_parser("init", help="初始化一个空的 Myfish 工作区目录")
    init_parser.add_argument("name", help="工作区文件夹名称 (example: my_bot_dir)")

    # run
    subparsers.add_parser(
        "run", help="启动机器人引擎 (自动挂载当前目录下的 plugins 和 data)"
    )

    sync_parser = subparsers.add_parser(
        "sync", help="迁移配置，并同步启用组件的运行依赖"
    )
    sync_parser.add_argument("--upgrade", action="store_true", help="更新锁定版本")

    config_parser = subparsers.add_parser("config", help="检查或迁移配置")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("check", help="预览配置变化和校验问题，不写入文件")
    config_commands.add_parser("sync", help="生成或迁移配置，不安装依赖")

    args = parser.parse_args()

    if args.command == "init":
        init_project(args.name)
    else:
        from myfish.core.config import (
            BotConfig,
            ConfigBuilder,
            ConfigManager,
            load_config,
        )
        from myfish.core.dependency import DependencyError, PluginDependencies

        try:
            if args.command == "run":
                run_bot()
            elif args.command == "sync":
                ConfigManager.sync_config()
                PluginDependencies(load_config()).sync(upgrade=args.upgrade)
                logger.success("插件依赖同步完成，执行 myfish run 启动")
            elif args.command == "config":
                if args.config_command == "sync":
                    ConfigManager.sync_config()
                else:
                    path = Path(os.getenv("MYFISH_CONFIG_PATH", "config.toml"))
                    compiled = ConfigBuilder(BotConfig).compile(path)
                    for entry in (*compiled.changes, *compiled.notes, *compiled.issues):
                        logger.info("{}", entry)
                    if compiled.changed or compiled.issues:
                        raise SystemExit(1)
                    logger.success("配置检查通过")
        except (DependencyError, OSError, ValueError) as exc:
            logger.error(str(exc))
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

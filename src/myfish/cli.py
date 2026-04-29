import argparse
import sys
import runpy
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

        readme_text = (
            f"# {project_name}\n\n"
            "这是一个基于 Myfish 框架的机器人工作区。\n\n"
            "- 🔌 **插件开发**: 请在`plugins/` 目录下新建单py文件或python的pkg, 框架启动时会自动导入。\n"
            "- 💾 **数据存储**: 账号凭证及其他持久化数据会自动存入 `data/` 目录。\n\n"
            "**🚀 一键启动:**\n"
            "在当前目录下执行命令：`myfish run`\n"
        )
        (target_dir / "README.md").write_text(readme_text, encoding="utf-8")

        logger.success(f"🎉 初始化 Myfish 工作区成功: {project_name}")
        logger.info(f"👉 下一步请执行:\n    cd {project_name}\n    myfish run")

    except Exception as e:
        logger.error(f"❌ 初始化失败: {e}")


def run_bot():
    """启动机器人引擎
    优先寻找当前目录下的 bot.py 作为入口，否则使用默认的 myfish.main入口
    """
    cwd = Path.cwd()
    bot_file = cwd / "bot.py"

    if str(cwd) not in sys.path:
        sys.path.insert(0, str(cwd))

    try:
        if bot_file.exists():
            logger.info("🔧 使用自定义的入口文件 bot.py 启动...")
            runpy.run_path(str(bot_file), run_name="__main__")
        else:
            logger.info("🐟 启用 Myfish默认的入口启动...")
            runpy.run_module("myfish.main", run_name="__main__")

    except KeyboardInterrupt:
        logger.info("bot已手动停止。")
    except Exception as e:
        logger.exception(f"❌ 运行过程中发生致命错误: {e}")


def main():
    """CLI 主路由"""
    parser = argparse.ArgumentParser(
        prog="myfish", description="🐟 Myfish: 现代化的闲鱼 Bot 开发框架"
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="可用命令")

    # init
    init_parser = subparsers.add_parser("init", help="初始化一个空的 Myfish 工作区目录")
    init_parser.add_argument("name", help="工作区文件夹名称 (example: my_bot_dir)")

    # run
    subparsers.add_parser(
        "run", help="启动机器人引擎 (自动挂载当前目录下的 plugins 和 data)"
    )

    args = parser.parse_args()

    if args.command == "init":
        init_project(args.name)
    elif args.command == "run":
        run_bot()


if __name__ == "__main__":
    main()

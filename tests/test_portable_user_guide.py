from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PortableUserGuideTests(unittest.TestCase):
    def test_package_includes_user_guide_and_close_command(self) -> None:
        build_script = (PROJECT_ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('"使用说明.txt"', build_script)
        self.assertIn('"关闭 LOF_iNAV.bat"', build_script)
        self.assertIn('"scripts\\stop_server.ps1"', build_script)
        self.assertTrue((PROJECT_ROOT / "packaging" / "PORTABLE_README.txt").is_file())
        self.assertTrue((PROJECT_ROOT / "packaging" / "STOP_LOF_iNAV.bat").is_file())

    def test_user_guide_covers_the_complete_portable_workflow(self) -> None:
        guide = (PROJECT_ROOT / "packaging" / "PORTABLE_README.txt").read_text(
            encoding="utf-8"
        )

        for section in (
            "免费开源与 MIT 许可证",
            "估值是怎样计算的",
            "使用前准备",
            "解压与首次启动",
            "日常启动和使用",
            "基金分类与筛选说明",
            "导出功能说明",
            "怎样看顶部状态",
            "正确关闭程序",
            "版本升级",
            "备份、移动和卸载",
            "常见问题",
            "隐私和网络说明",
        ):
            with self.subTest(section=section):
                self.assertIn(section, guide)

        self.assertIn("不要直接在 ZIP 压缩包内运行", guide)
        self.assertIn("关闭浏览器标签页或浏览器窗口，不会关闭 LOF iNAV", guide)
        self.assertIn("不需要把旧版 data 文件夹复制到新版", guide)
        self.assertIn("不构成投资建议", guide)
        self.assertIn("不对任何个人因", guide)
        self.assertIn("必须在软件的所有副本或主要部分中保留", guide)
        self.assertIn("最后一次公布的单位净值", guide)
        self.assertIn("不使用美股盘前、盘后或其他股票夜盘数据", guide)
        self.assertIn("全天交易时段的实时行情", guide)
        self.assertIn("商品-贵金属", guide)
        self.assertIn("商品-原油", guide)
        self.assertIn("当前排序后的行顺序", guide)
        self.assertIn("页面下方的持仓/代理资产和逐条回测详情不会进入导出", guide)


if __name__ == "__main__":
    unittest.main()

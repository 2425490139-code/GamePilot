# 来源与归属

GamePilot 为非官方的鸣潮周常乐园辅助项目，与游戏官方无关联。

## 设计参考

- [OK-WW](https://github.com/ok-oldking/ok-wuthering-waves)：GardenTask.py 的页面特征驱动流程与候选选择思路。
- [ok-script](https://github.com/ok-oldking/ok-script)：窗口消息交互方式的设计参考。
- [BetterGI](https://github.com/babalae/better-genshin-impact)：目标红框、窗口最小化与任务取消交互参考。

开发记录声明上述内容用于设计参考，未直接引入上游函数或图片模板；本说明不替代对后续引入代码的许可证核对。

## 素材

gamepilot/templates 中的识别图片及 tests/fixtures 中的局部测试裁剪来自用户提供的鸣潮游戏界面，相关游戏画面及标识归各自权利人，不作为项目自有原创素材授权。
gamepilot/assets 中的声波、共鸣环图标与界面装饰由本项目代码绘制。
完整游戏截图、用户标识、历史日志与本机配置未加入本仓库。

## 依赖

NumPy、Pillow、OpenCV、PyInstaller、Python/Tk 各自适用其上游许可证。打包/再分发时须遵守对应版本的许可与声明要求。

本仓库尚未选择自有代码的通用许可证，不表示获得上游代码、游戏素材或商标的额外授权。

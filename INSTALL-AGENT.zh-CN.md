# 给负责安装和维护 PanMesh 的 agent

[English](INSTALL-AGENT.md) · 简体中文

用户把这份文件交给你时，请完成本机安装、指定客户端接入和验证。确认安装目录、项目实际路径、要接入的客户端、各自模式和负责人。用用户指定的当前聊天担任打理入口；以后换聊天时，将安装路径、项目路径、连接名、角色、渠道与未完成任务编号交给新负责人。身份和记录保存在本机，不绑定某个模型的原生记忆。

## 安装

当前开箱路径为 Windows、Git、Python 3.11。以下命令从用户选定的新安装目录执行。已有安装先核对现状和用户数据；不要覆盖库。下载源码示例：

```powershell
git clone https://github.com/Pan-Shufeng/PanMesh.git D:\PanMesh
cd D:\PanMesh
py -3.11 -m venv runtime/python
.\runtime\python\Scripts\python.exe -m pip install -r requirements.txt
```

把 vault 作为 Obsidian 库打开是可选步骤。不要把 imports、运行数据库、凭据或备份提交到公开仓库。

## 接入用户指定项目

用安装目录里的 manage.py。下面以同一个论文项目的 Codex 主负责、Cursor 执行为例。工作目录须已存在。连接名用小写英文、数字或短横线，多个项目分别命名，第二次运行先核对已登记身份。

```powershell
$py = ".\runtime\python\Scripts\python.exe"
& $py manage.py enroll papers-main papers codex collab
& $py manage.py enroll papers-worker papers cursor collab
& $py manage.py link-project papers-main codex "D:\论文整理"
& $py manage.py link-project papers-worker cursor "D:\论文整理"
& $py manage.py route papers-main cursor papers-worker cursor "D:\论文整理" --channel pull
& $py project_client.py list papers-main
```

Claude Code 的 link-project 客户端名为 claude；桌面 Code 窗口主动领单的路由客户端名为 claude-desktop。只采集的连接在 enroll 时用 capture，跳过派工路由。其他安装助手可以执行这些命令；其他执行客户端须先确认其能使用本机 stdio MCP 或有可用适配器。只将连接加入选中的项目。现有同名配置发生冲突时先识别原绑定。

让客户端加载项目配置，在实际窗口检查本服务工具。协作连接用 task_list 只读验证，只采集连接列工具应只看到 memory_capture。分别报告本机服务、项目配置和实际窗口连通状态。用户若授权协作演练，用一条合成任务验证送达和回报。模型按目标客户端实际菜单或适配器选择；不可用时让用户决定替代。

交付时给用户安装位置、各窗口的模式及 [主对话说明](COLLABORATE.zh-CN.md)。以后的打理工作包括项目增删、权限、版本提案和备份。备份用 manage.py backup，恢复用 manage.py restore 快照目录 新目录；恢复后重新登记连接。用户指定的聊天或材料才进入长期库。普通协作窗口可独立于打理聊天，沿用相应项目绑定。

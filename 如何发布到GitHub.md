# 如何把本仓库发布到 GitHub（新手版）

> 目标：把这个文件夹变成一个公开的 GitHub 仓库，拿到一个链接放简历。

## 一、准备工作

1. 注册 GitHub 账号：https://github.com/signup （用户名建议用 `vanko-zhou` 这类，专业、好记）。
2. 安装 Git：https://git-scm.com/downloads （安装时一路默认即可）。
3. 打开命令行（Windows 按 `Win + R`，输入 `cmd` 回车），验证安装：

```bash
git --version
```

## 二、在 GitHub 上创建空仓库

1. 登录 GitHub，点右上角 `+` → `New repository`。
2. **Repository name** 填：`openclaw-leadgen-workflow`
3. Description 填：`Automate overseas lead generation with a locally-deployed OpenClaw agent`
4. 选择 **Public**（公开，面试官才看得到）。
5. **不要**勾选 "Add a README file"（我们已经有自己的 README）。
6. 点 `Create repository`。

## 三、把本地文件夹推上去

在命令行里依次执行（把路径替换成你的实际路径）：

```bash
cd "C:\Users\Administrator\Desktop\求职\openclaw-leadgen-workflow"

git init
git add .
git commit -m "docs: initial commit - OpenClaw lead generation workflow"

git branch -M main
git remote add origin https://github.com/你的用户名/openclaw-leadgen-workflow.git
git push -u origin main
```

> 第一次 push 时会弹出登录窗口，按提示登录 GitHub 即可。

## 四、完成后的样子

仓库地址形如：`https://github.com/你的用户名/openclaw-leadgen-workflow`

把这个链接填到：
- **简历**的"作品集 / 链接"处；
- **作品集网站**的 GitHub 一栏。

## 五、⚠️ 上传前务必检查

- [ ] 仓库里**没有任何账号密码、API Key**（本仓库已用 `.gitignore` 挡住常见密钥文件）；
- [ ] 没有涉及前公司的**内部资料、客户名单、报价**等敏感内容；
- [ ] 提示词里的客户信息已脱敏（用「某公司」代替真实客户名）。

## 六、加分项（可选，做完面试通过率更高）

- 录一段 **1-2 分钟**的屏幕操作视频，展示工作流跑起来的样子，传到 B 站 / YouTube，在 README 里加个链接；
- 补一份 `examples/` 文件夹，放一份**脱敏后**的输入名单与输出表格样例；
- 在 README 顶部加一行 **badge**（如 `MIT License`）。

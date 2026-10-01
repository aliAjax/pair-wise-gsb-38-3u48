# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核，分配的非创建人复核人批准或退回。
6. 负责人锁定已批准版本：系统按三个渠道的规则分别生成字幕包和逐文件 SHA-256 清单。
   - 影院 `cinema`：DCP 风格 `subtitles.xml`（仅字幕）。
   - 流媒体 `streaming`：`subtitles.vtt` + `glossary.json`。
   - 电视台 `tv`：`subtitles.srt` + `glossary.json`。
7. 每个渠道包独立状态：`pending` → `packed` → `confirmed`（异常为 `failed` / `invalidated`）。
   - 某个渠道打包失败或中断时，已完成渠道保留，`POST /api/versions/{id}/deliver` 只续打未完成项。
   - 重复锁定/重复续打是幂等的，不会生成第二份包（`UNIQUE(delivery_id,channel)`）。
   - 每个包带内容指纹：改字幕失效全部渠道；改术语只失效含术语表的流媒体和电视台；再次锁定时只有指纹变化的包会重打。
   - 交付完成前负责人可 `unlock` 解锁返修，返修后需重新提交、复核、锁定。
8. 渠道回执到达后按清单逐文件比对校验值：缺件或校验不符时该渠道保持 `packed` 且回执记为 `mismatched`；全部渠道对账成功版本才置为 `delivered`。同一回执号重复到达只记一次。
9. 旧版单快照交付在数据库初始化时按其现有清单确定性补建三个渠道记录和 `legacy` 回执，不覆盖任何旧数据。同语言的新交付会把旧的已交付版本标记为 `superseded`，旧快照不会删除或覆盖。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词（改动后相关渠道包自动失效）。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|unlock|deliver`：复核状态机与渠道打包（`deliver` 为断点续打）。
- `POST /api/deliveries/{id}/channels/{cinema|streaming|tv}/receipt`：提交渠道回执，请求体 `{"receipt_no":"R-1","files":[{"path":"subtitles.vtt","sha256":"..."}]}`；返回 `reconciled`、`missing_files`、`mismatched_files` 和 `duplicated`。
- `GET /api/versions/{id}/cues|comments|delivery`、`GET /api/deliveries`、`GET /api/deliveries/{id}`：查看结果与每个渠道的状态。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限。

# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和多渠道交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见、按渠道拆分的交付包和渠道回执。

## 运行

```bash
python3 app.py --init
python3 app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本和术语规则（术语可用 `channels` 限定只下发给某些渠道）。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕（`sdh: true` 标记听障字幕）；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核，分配的非创建人复核人批准或退回。
6. 负责人**锁定**已批准版本：系统为三个渠道各自生成独立字幕包和清单，每个渠道独立事务提交：
   - `cinema` 影院：DCP XML，排除 SDH 字幕；
   - `streaming` 流媒体：WebVTT，含 SDH；
   - `tv` 电视台：EBU-STL 文本包，含 SDH。
   
   每个包的清单列出各文件（字幕、术语切片）的 SHA-256 和整体 `package_hash`。
7. 某个渠道打包失败只把该渠道置为 `failed`（可重试），已完成渠道保持 `packaged`。调用 `POST /api/versions/{id}/packages` 重试：只有 `pending/failed/stale` 的渠道会重新生成，`packaged` 的不会出第二份包；重复请求幂等。
8. 锁定后如发现字幕或术语问题，负责人可 `unlock` 回草稿修改：只有按渠道规则真正受影响的包变为 `stale`（影院不含 SDH，改 SDH 不影响影院；渠道限定术语只影响相关渠道），旧回执同步作废，重试时只重建受影响渠道。
9. 各渠道回执到达后登记到 `receipts`：按回执内的文件校验值与包清单逐文件对账。缺件、校验不符或多出文件记为 `mismatched`；重复回执号只记一次（返回原记录，`deduplicated: true`）。
10. 三个渠道全部 `packaged` 且都有有效的 `matched` 回执时，负责人才可确认交付，版本转为 `delivered` 并生成聚合快照；任一渠道缺件或校验不符返回 409 及具体阻塞渠道。同语言的新交付会把旧版本标记为 `superseded`，历史快照不删除。
11. 升级前已存在的旧版单份交付，在服务启动时按其原有清单确定性地补建三个渠道包记录和 legacy 回执（标记 `legacy: true`），补建幂等。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## 负责人可见的渠道状态

`GET /api/versions/{id}/packages` 返回版本状态和每个渠道的：`status`（`missing/pending/packaged/failed/stale`）、`package_hash`、`manifest_sha`、文件清单与校验值、`error`、最近一次回执及对账结果、回执数量。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词，可带 `channels: ["cinema", ...]`。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`，可带 `sdh`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review`：提交复核、批准或退回。
- `POST /api/versions/{id}/lock`：锁定版本并生成三渠道包（失败渠道标记 `failed`，不影响其他渠道）。
- `POST /api/versions/{id}/unlock`：未交付的锁定版本解锁回草稿，按规则失效受影响渠道包。
- `POST /api/versions/{id}/packages`：按渠道重试/补生成（body 可带 `channels` 子集；幂等）。
- `POST /api/versions/{id}/receipts`：登记渠道回执 `{channel, receipt_id, files:[{path, sha256}, ...]}`，自动对账，重复回执只记一次。
- `POST /api/versions/{id}/deliver`：三方回执全部对账通过后确认交付。
- `GET /api/versions/{id}/packages`、`GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看状态与结果。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、渠道规则差异（格式/SDH/术语切片）、选择性失效与重试续跑、重复包与重复回执幂等、缺件与校验不符阻塞交付、旧交付清单补建以及 HTTP 接口。

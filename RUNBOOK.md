# 运行手册 — 波动率交易案例

脚本必须和 RIT Client 跑在**同一台 Windows 机器**上。Client 的 REST API 只监听 `localhost:9999`，
Mac 上的脚本连不到 Windows 上的 Client。所以：**代码和 Client 都在 Windows，Mac 只用来写代码和 push。**

---

## 零、连接信息

**练习服务器**（2026-09-11 12PM 起启用，**竞赛日当天关闭**）：

| 案例 | Windows RIT Client | Browser / Mac App (DMA) |
| --- | --- | --- |
| Volatility Trading | `flserver.rotman.utoronto.ca:16590` | `:16595` |
| Algorithmic ETF Arb | `flserver.rotman.utoronto.ca:16630` | `:16635` |

**同一个案例两个端口,别搞混**：RIT Client 登录用 **16590**，DMA/浏览器用 16595。
用 16595 去登 Client 是连不上的。

登录 RIT Client 填：host `flserver.rotman.utoronto.ca`、port `16590`、Trader ID 和 Password
（练习服务器用邮件里发的那组；练习凭证是 `tqdu-1`）。

> **竞赛日**：练习服务器会被关闭，竞赛端口只在当天开放，
> 并且主办方会另发一组竞赛专用的 Trader ID 和 Password。
> 当天第一件事是把新的 host / port / 凭证填进去，不要沿用练习的。

主办方明确推荐 **Client-based REST API**（比 DMA 更稳定），所以 Windows + RIT Client + REST 是对的路线。

---

## 一、一次性设置（Windows，做一次就行）

1. **装 RIT Client**（只有 Windows 版才提供 `localhost:9999` 的 REST API）

   - 下载页：<https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/rit-market-simulator/rit-downloads/>
   - 官方操作手册（有截图）：<https://rotmanfrtl.github.io/RIT%20User%20Application%20(RIT%20Client)%20Feature%20Guide.pdf>

   装好启动后，在登录框里填：

   | 字段 | 值 |
   | --- | --- |
   | Server / Host | `flserver.rotman.utoronto.ca` |
   | Port | `16590` |
   | User / Trader ID | 邮件里发的（练习是 `tqdu-1`） |
   | Password | 邮件里发的 |

   登录成功的判断标准：能看到 RTM 和 10 个期权的行情在跳。只是界面打开不算。

   > 不想装东西先试试的话，浏览器版在 <https://client.rotmanrit.com:14980/>，
   > 也有 Mac app。但**这两个走的是 DMA（16595），不提供 localhost:9999**，
   > 所以只能用来看行情和人工下单，跑不了 REST 脚本。
2. **装 Python 3.11 或更高**。安装时务必勾选 **Add Python to PATH**。
3. **拿代码**：

   ```
   git clone https://github.com/MuLancer/MSCF-Trading-Competition.git
   cd MSCF-Trading-Competition/code
   ```

4. **装依赖**：

   ```
   pip install -r requirements.txt
   ```

5. **验证装好了**（不需要 Client 开着）：

   ```
   python test_vol_strategy.py
   ```

   应该看到 13 个 PASS。任何一个 FAIL 都说明环境有问题，先解决再往下走。

---

## 二、赛前测试流程（最关键，别留到当天）

**前提：RIT Client 已启动、已登录、已连上一个练习案例。**

### 第 1 步：验证连通性和字段名 ← 最重要

```
jupyter notebook test_news.ipynb
```

依次跑 cell，重点看两个：

- **cell 3**（`/news` 返回）：字段名必须是 `news_id / period / tick / ticker / headline / body`
- **cell 6**（`/securities` 返回）：必须有 `ticker / last / bid / ask / position`

**如果字段名对不上，`vol_strategy.py` 会直接 KeyError。** 这是唯一一个只能在真机上发现的问题，
务必在比赛前跑通。字段名不一样就告诉我，改起来很快。

cell 5 会用真实新闻文本测波动率解析 —— 确认能正确抽出百分比。

### 第 2 步：空跑一整场

`vol_strategy.py` 顶部的 `DRY_RUN = True`（默认就是），这时脚本只打印要下的单，**不会真的下单**。

```
python vol_strategy.py
```

启动一个练习案例，让它跑完整 300 tick，盯这几件事：

- `vol=` 有没有随着新闻公告变化（不变说明新闻解析没生效）
- `signals=` 数量是否合理（一直是 0 说明阈值太高；一直是 10 说明太低）
- `delta=` 有没有被拉回 ±5000 以内
- `[DRY]` 那些行的方向对不对（波动率被高估时应该是 SELL）

### 第 3 步：真实下单跑一场

把 `DRY_RUN` 改成 `False`，再跑一场练习案例。看 RIT Client 里的实际持仓、P&L 和 delta，
跟脚本打印的对得上不对得上。

### 第 4 步：调参

根据第 3 步的结果调 `IV_GAP_THRESHOLD` 和 `DELTA_BAND`，多跑几场。

---

## 三、比赛当天

### 开赛前 30 分钟

- [ ] Windows 机器开机，插电
- [ ] `git pull` 拿最新代码
- [ ] 启动 RIT Client，登录
- [ ] `python test_vol_strategy.py` → 13 个 PASS
- [ ] 打开 `vol_strategy.py`，确认 **`DRY_RUN = False`**
- [ ] 确认 `RISK_FREE` 是否需要改（主办方可能改无风险利率）
- [ ] 确认参数是调好的那组值

### 开赛前 5 分钟

- [ ] 在 `code/` 目录下开好命令行，命令敲好但别回车
- [ ] Client 里确认连的是**正确的案例**

### 开赛

案例状态变成 ACTIVE 后立刻：

```
python vol_strategy.py
```

### 运行中盯什么

日志每个 tick 一行：

```
tick=42 vol=0.200 delta=-3,180 signals=3
```

- **`delta` 绝对值超过 7000 且不回落** → 立刻 Ctrl+C，在 Client 里手动平仓。罚款是每秒 $0.10 × 超出量，
  停在 10000 上整场就是 $90,000。
- **`vol` 一直不变** → 新闻解析挂了，但不影响已有持仓，可以让它跑完
- **刷屏报 API error** → Ctrl+C，看报错信息

### 结束

Ctrl+C 优雅退出。未平仓位会按最后成交价自动结算，不需要手动平。

### 多轮之间

规则允许改算法。每轮之间可以改参数重跑 —— 改完存盘，下一轮直接重新 `python vol_strategy.py`。

---

## 四、出问题怎么办

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `ConnectionError` / Connection refused | Client 没开，或没登录 | 启动 Client 并登录 |
| `401` + Auth failed | API key 不对 | 确认是 `X-API-Key: Rotman` |
| `KeyError: 'bid'` 之类 | 字段名跟文档不一致 | 照真机返回改 `build_signal_table` |
| 刷屏 `Rate limit exceeded` | 请求太密 | 把 `LOOP_SLEEP` 从 0.25 调到 0.5 |
| delta 一直超限 | 对冲单被拒或没成交 | Ctrl+C，Client 里手动平 |
| 完全不下单 | 阈值太高，或 `DRY_RUN` 忘了关 | 检查 `DRY_RUN`，再看 `signals=` |

**任何情况下最后的兜底是 Ctrl+C + 在 RIT Client 里手动平仓。** Client 一直开着，人工随时能接管。

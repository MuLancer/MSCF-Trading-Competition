# 运行手册 — 波动率交易案例（Windows + RIT Client REST API）

主办方推荐这条路线（比 DMA 稳定）。**脚本和 RIT Client 必须在同一台 Windows 机器上**——
Client 只在本机开 `localhost:9999`，Mac 上的脚本连不到 Windows 上的 Client。

> 浏览器版 RIT 和 Mac app **不会**开 `localhost:9999`，它们走 DMA。
> 登录浏览器版对 REST 脚本没有任何帮助。

---

## 零、连接信息

**练习服务器**（竞赛日当天会关闭）：

| 案例 | Windows RIT Client | Browser / Mac App (DMA) |
| --- | --- | --- |
| Volatility Trading | **16590** | 16595 |
| Algorithmic ETF Arb | **16630** | 16635 |

Host 都是 `flserver.rotman.utoronto.ca`。

**同一个案例两个端口，别搞混**：Client 登录用 **16590**，16595 是给浏览器/Mac app 的。

> **竞赛日**：练习服务器关闭，竞赛端口只在当天开放，主办方另发一组竞赛专用
> Trader ID 和 Password。当天第一件事是换 host / port / 凭证，不要沿用练习的。

---

## 一、Windows 一次性设置

### 1. 装 RIT Client

- 下载页：<https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/rit-market-simulator/rit-downloads/>
- 官方操作手册（有截图）：<https://rotmanfrtl.github.io/RIT%20User%20Application%20(RIT%20Client)%20Feature%20Guide.pdf>

### 2. 登录 Client

| 字段 | 值 |
| --- | --- |
| Server / Host | `flserver.rotman.utoronto.ca` |
| Port | **`16590`** |
| User / Trader ID | 邮件里发的 |
| Password | 邮件里发的 |

**登录成功的判断标准：能看到 RTM 和 10 个期权的行情在跳。** 界面打开不算。
`localhost:9999` 只有在 Client 真正连上案例之后才开始服务——之前的 401 就是这个原因。

### 3. 装 Python 环境

装 Python 3.11+，安装时**务必勾选 Add Python to PATH**。然后：

```
git clone https://github.com/MuLancer/MSCF-Trading-Competition.git
cd MSCF-Trading-Competition\code
pip install -r requirements.txt
```

### 4. 确认环境没问题（不需要 Client）

```
python test_vol_strategy.py
```

应该看到 14 个 PASS。任何 FAIL 都先解决再往下走。

### 5. 确认 MODE

打开 `code\vol_strategy.py`，确认顶部是：

```python
MODE = "client"
```

这是默认值，Windows 上不用改，也不需要设任何环境变量。

---

## 二、赛前测试（用练习服务器，别留到当天）

**前提：Client 已登录 16590 并且能看到行情在跳。**

### 第 1 步：验证连通和字段名

```
jupyter notebook test_news.ipynb
```

从上往下跑。cell 2 出现 `HTTP 200` 就说明 REST 通了。重点看：

- **cell 4**：`/news` 字段应为 `news_id / period / tick / ticker / headline / body`
- **cell 6**：波动率解析结果，每条公告都应该解析出数字而不是 `None`
- **cell 7**：`/securities` 必须含 `ticker / last / bid / ask / position`

字段名对不上就先别跑主脚本，告诉我，改起来很快。

### 第 2 步：空跑一整场

`vol_strategy.py` 顶部 `DRY_RUN = True`（默认），这时只打印要下的单，**不会真下单**。

```
python vol_strategy.py
```

启动一个练习案例跑满 300 tick，盯四件事：

- `vol=` 有没有随新闻变化（不变说明新闻没解析到）
- `signals=` 数量（一直 0 说明阈值太高；一直 10 说明太低）
- `delta=` 有没有被拉回 ±5000 以内
- `[DRY]` 行的方向对不对（波动率被高估时应该 SELL）

### 第 3 步：真实下单

把 `DRY_RUN` 改成 `False`，再跑一场。对照 Client 里的实际持仓、P&L、delta，
看跟脚本打印的对不对得上。

### 第 4 步：调参

根据结果调 `IV_GAP_THRESHOLD` 和 `DELTA_BAND`，多跑几场练习。

---

## 三、比赛当天

### 开赛前 30 分钟

- [ ] Windows 开机、插电、关掉自动更新和休眠
- [ ] `git pull`
- [ ] 启动 RIT Client，用**当天的** host / port / 凭证登录
- [ ] 确认能看到行情在跳
- [ ] `python test_vol_strategy.py` → 14 个 PASS
- [ ] `vol_strategy.py` 里确认 `MODE = "client"`、**`DRY_RUN = False`**
- [ ] 确认 `RISK_FREE`（主办方可能改无风险利率，开场公告会说）
- [ ] 确认参数是调好的那组

### 开赛前 5 分钟

- [ ] 在 `code` 目录开好命令行，`python vol_strategy.py` 敲好但别回车
- [ ] Client 里确认连的是正确的案例

### 开赛

案例状态变 ACTIVE 后立刻回车。

### 运行中盯什么

日志每 tick 一行：

```
tick=42 vol=0.230 delta=-3,180 signals=3
```

**只有一件事需要紧盯：`delta` 绝对值超过 7000 且不回落 → 立刻 Ctrl+C，在 Client 里手动平仓。**
罚款每秒 $0.10 × 超出量，停在 10000 上跑完整场就是 $90,000。

- `vol` 一直不变 → 新闻解析挂了，但不影响已有持仓，可以让它跑完
- 刷屏 API error → Ctrl+C 看报错

### 结束

Ctrl+C 退出。案例**跑完**之后未平仓位会自动结算，不用手动平。

> ### ⚠️ 案例进行中 Ctrl+C，必须立刻处理仓位
>
> 脚本一停就没有人对冲了，delta 停在哪就是哪，而罚款**按秒继续计**。
>
> 实测：一轮在 tick 148 被 Ctrl+C，仓位留着没管，delta 在 −20,000 到 −35,000
> 停了剩下的 150 个 tick，罚款从 0 涨到 **74,309**，比那一轮赚到的 65,716 还多 ——
> 一个本来盈利的 heat 直接变成净亏。
>
> 所以中途停下来之后，二选一：
>
> - **马上重启** `python vol_strategy.py`，或者
> - **平掉** `python flatten.py --live`
>
> 脚本退出时如果检测到"案例仍在进行 + 仍有持仓"会打印醒目警告，看到就照做。

### 多轮之间

规则允许改算法。改完存盘，下一轮直接重新 `python vol_strategy.py`。

> **评分是排名制**：每个 heat 按团队 P&L 排名，取各 heat **排名的平均值**定最终名次，
> 明确说了是为了防止赌一把。所以**稳定比爆发重要**，别为了某一轮翻盘去放大仓位。

---

## 四、出问题怎么办

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `ConnectionError` / Connection refused | Client 没开，或没登录进案例 | 登录 Client，确认行情在跳 |
| `401` | Client 未连上案例；或 API key 不对 | 先确认行情在跳；再查 Client 里的 API key |
| `KeyError: 'bid'` 之类 | 字段名和文档不一致 | 照真机返回改 `build_signal_table` |
| 刷屏 `Rate limit exceeded` | 请求太密 | `LOOP_SLEEP` 从 0.25 调到 0.5 |
| delta 一直超限 | 对冲单被拒或没成交 | Ctrl+C，Client 里手动平 |
| 完全不下单 | 阈值太高，或 `DRY_RUN` 忘了关 | 查 `DRY_RUN`，再看 `signals=` |

### 紧急平仓

出事要立刻清空仓位时，**不要在 Client 里一条条手点**——开另一个命令行窗口：

```
cd MSCF-Trading-Competition\code
python flatten.py            # 先看它要做什么，不发单
python flatten.py --live     # 确认后执行
```

会按 ticker 自动拆单（期权 100 张/笔、RTM 10,000 股/笔），完成后复查是否真的清零。
连接配置跟主脚本共用，不用重复填。

> **到期时不需要跑这个。** 期权按内在价值自动现金结算、RTM 按最后成交价自动平仓，
> 主动去平只是白付手续费和点差。这个脚本是给"出事了要马上撤"和"清掉练习仓位"用的。

**最终兜底是 Ctrl+C + `python flatten.py --live`。** Client 一直开着，人工也随时能接管。

---

## 附：在 Mac 上测试（备用）

Mac 上没有 Client，只能走 DMA。改 `MODE = "dma"`，并且**凭证从环境变量传**
（这个仓库是 public，不要写进文件）：

```bash
export RIT_USER=xxxx-1
export RIT_PASS=yyyy
python vol_strategy.py
```

DMA 用 16595。主要用途是没有 Windows 时验证策略逻辑。

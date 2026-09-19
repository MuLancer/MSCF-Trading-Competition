# 运行手册

比赛当天照着这份念。策略原理见 [README.md](README.md)。

**脚本和 RIT Client 必须在同一台 Windows 机器上。** Client 只在本机开
`localhost:9999`,Mac 上的脚本连不到 Windows 上的 Client。浏览器版 RIT 和
Mac app **不会**开这个端口,登录它们对 REST 脚本没有任何帮助。

---

## 零、连接信息

**练习服务器**(竞赛日当天关闭):

| 案例 | Windows Client | DMA(浏览器/Mac) |
| --- | --- | --- |
| Volatility Trading | **16590** | 16595 |
| Algorithmic ETF Arb | **16630** | 16635 |

Host 都是 `flserver.rotman.utoronto.ca`。

> **两个案例各自有一组凭证,不通用。** 同一案例的两个端口也别搞混:
> Client 用 16590/16630,另一个是给 DMA 的。

> **竞赛日**:练习服务器关闭,竞赛端口只在当天开放,主办方另发一组凭证。
> 当天第一件事是换 host / port / 凭证,不要沿用练习的。

---

## 一、Windows 一次性设置

### 1. 装 RIT Client

- 下载页:<https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/rit-market-simulator/rit-downloads/>
- 官方手册(有截图):<https://rotmanfrtl.github.io/RIT%20User%20Application%20(RIT%20Client)%20Feature%20Guide.pdf>

登录填 host、上表的 port、Trader ID、Password。

**登录成功的判断标准:能看到行情在跳。** 界面打开不算 ——
`localhost:9999` 只有在 Client 真正连上案例之后才开始服务。

### 2. 装 Python

装 Python 3.11+,**务必勾选 Add Python to PATH**。然后:

```
git clone https://github.com/MuLancer/MSCF-Trading-Competition.git
cd MSCF-Trading-Competition\code
pip install -r requirements.txt
```

### 3. 确认环境(不需要 Client)

```
python test_vol_strategy.py     -> 30 个 PASS
python test_etf_strategy.py     ->  8 个 PASS
```

任何 FAIL 都先解决再往下走。

---

## 二、赛前测试(别留到当天)

### 波动率案例

**1. 验证连通和字段名**

```
jupyter notebook test_news.ipynb
```

cell 2 出现 `HTTP 200` 说明 REST 通了。重点核对:

- `/news` 字段是 `news_id / period / tick / ticker / headline / body`
- 每条公告都解析出数字,不是 `None`
- `/securities` 含 `ticker / last / bid / ask / position`

**2. 空跑一整场**(`DRY_RUN = True`,只打印不下单)

```
python vol_strategy.py
```

盯四件事:

- `vol=0.310->0.212` 箭头后是实际定价用的值,周中公告后应该明显变化
- `delta=` 应该基本在 ±3,000 内
- `signals=` 一直 0 说明没机会(正常),一直 10 且 `room=0` 说明额度用光
- 不应出现 `hedge blocked`

**3. 真实下单跑一场**,对照 Client 里的持仓和 P&L。

### ETF 案例

```
python etf_strategy.py --check      # 只读，不下单
```

核对**字段名**、**限额权重**、**当前套利边际**。

> 权重和上限是运行时从 API 读的。练习服务器上 RITC 权重 **0.5**、gross 上限
> **300,000** —— 都和案例 PDF 写的(×2)不一致。照 PDF 算套利容量会少四成。

确认无误后把 `DRY_RUN` 改成 `False`。

---

## 三、比赛当天

### ETF 监控与复盘

另开一个窗口运行只读监控；它不会下单，并把 ETF 每 tick 写入
`code/etf_ticks.csv`:

```
cd code
python3 -u etf_monitor.py
```

结束后查看各 heat 的 P&L、回撤和机会次数:

```
python3 etf_monitor.py --review
```

波动率案例仍使用 `monitor.py` 和 `ticks.csv`，两套记录互不覆盖。

### 开赛前 30 分钟

- [ ] Windows 开机、插电、**关掉自动更新和休眠**
- [ ] `git pull`
- [ ] 启动 RIT Client,用**当天的** host / port / 凭证登录
- [ ] 确认行情在跳
- [ ] `python test_vol_strategy.py` → 30 PASS
- [ ] `python test_etf_strategy.py` → 8 PASS
- [ ] 两个脚本都确认 `MODE = "client"`、**`DRY_RUN = False`**
- [ ] 波动率:确认 `RISK_FREE`(主办方可能改,开场公告会说)
- [ ] ETF:跑一次 `--check` 确认权重

### 开赛前 5 分钟

- [ ] 命令行开好,命令敲好**别回车**
- [ ] 另开一个窗口跑 `python -u monitor.py`(只读,记录用)
- [ ] Client 里确认连的是正确的案例

### 开赛

状态变 ACTIVE 后立刻回车。

### 运行中盯什么

**波动率案例 —— 只有一件事需要紧盯:**

```
tick=42 wk=1 vol=0.310->0.212 r=0.000 delta=-3,180 signals=3 room=430
```

**`delta` 绝对值超过 7,000 且不回落 → 立刻 Ctrl+C 并平仓。**
罚款每秒 $0.10 × 超出量,停在 30,000 上一分钟就是三万多。

**ETF 案例 —— 需要你动手:**

接受大额 tender 时脚本会打印:

```
************************************************************
HUMAN: press ETF-Redemption 4 times in the Assets tab.
The plan only works if 40,000 shares go through the converter;
the books cannot absorb them.
************************************************************
```

去 **Assets – Converters** 点对应次数,每次 2 个 tick 完成:

| | 投入 | 产出 |
| --- | --- | --- |
| ETF-Redemption | 10,000 RITC + 1,500 USD | 10,000 BULL + 10,000 BEAR |
| ETF-Creation | 10,000 BULL + 10,000 BEAR + 1,500 USD | 10,000 RITC |

**不点的后果:** 仓位平不掉扛到收盘,而且脚本的成交均价里已经算进了换算,
不点的实际结果会比它报的更差。

### 结束

Ctrl+C 退出。案例**跑完**之后仓位自动结算,不用手动平。

> ### ⚠️ 案例进行中 Ctrl+C,必须立刻处理仓位
>
> 脚本一停就没人对冲了,delta 停在哪是哪,罚款**按秒继续计**。
>
> 实测:一轮在 tick 148 被 Ctrl+C,仓位留着没管,delta 在 −20,000 到 −35,000
> 停了剩下 150 个 tick,罚款从 0 涨到 **74,309**,比那轮赚的 65,716 还多 ——
> 一个盈利的 heat 直接变净亏,**全程没有发生任何一笔交易**。
>
> 中途停下来之后二选一:
>
> - **马上重启**脚本,或者
> - **平掉**:波动率案例 `python flatten.py --live`;ETF 案例在 Client 里手动平
>
> 脚本退出时若检测到"案例仍在进行 + 仍有持仓"会打印醒目警告,看到就照做。

### 多轮之间

规则允许改算法。**改完必须重启进程** —— Python 启动时就把模块读进内存了,
改文件不影响正在跑的进程。曾经有一整轮是在调试九分钟前就已经修好的问题。

用 `python monitor.py --review` 看累计战绩和归因。

> **评分是排名制**:每轮按团队 P&L 排名,取各轮**排名的平均值**定最终名次,
> 两个案例各占 50%。所以**稳定比爆发重要**,别为了某轮翻盘去放大仓位。

---

## 四、出问题怎么办

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `ConnectionError` | Client 没开,或没登录进案例 | 登录 Client,确认行情在跳 |
| `401` | Client 未连上案例;或案例还没 ACTIVE | 先确认行情在跳 |
| `401 on /orders` | 案例还没开始 | 脚本会自己等,不用管 |
| `KeyError` | 字段名和预期不一致 | 把真机返回发出来,改起来很快 |
| 刷屏 `Rate limit` | 请求太密 | `LOOP_SLEEP` 调大一倍 |
| `hedge blocked` | 对冲腿顶到仓位上限 | 说明期权仓位过大,调小 `MAX_OPTION_DELTA` |
| delta 一直超限 | 对冲失效 | Ctrl+C,平仓 |
| 完全不下单 | `DRY_RUN` 忘了关,或没机会 | 查 `DRY_RUN`,再看 `signals=` |
| 改了参数没效果 | **进程还是旧代码** | Ctrl+C 重启 |

### 紧急平仓(波动率案例)

```
python flatten.py            # 先看它要做什么，不发单
python flatten.py --live     # 确认后执行
```

自动按 ticker 拆单,完成后复查是否清零。

> **到期时不需要跑这个** —— 期权自动现金结算、标的自动平仓,主动去平只是
> 白付手续费。它是给"出事要马上撤"和"清练习仓位"用的。
>
> ETF 案例用不了这个脚本(它读的是波动率的配置),直接在 Client 里平。

**最终兜底永远是 Ctrl+C + 在 RIT Client 里手动平仓。**

---

## 附:在 Mac 上测试

Mac 没有 Client,只能走 DMA。改 `MODE = "dma"`,**凭证从环境变量传**
(仓库是 public,不要写进文件):

```bash
export RIT_USER=xxxx
export RIT_PASS=yyyy
python vol_strategy.py
```

主要用途是没有 Windows 时验证策略逻辑。

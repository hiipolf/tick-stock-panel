"""财联社 (CLS) Provider 插件.

vendor 规范: `docs/plugin-development.md` §"快速上手"
- 目录: backend/app/plugins/<your_plugin>/
- 必须: plugin.yaml + provider.py
- 可选: client.py / bridge.py / tests/

协议已按 2026-10-19 **真实连跑** 校正 (12/12 接口返回 200), 不是凭文档猜的:
- 两套签名: web 端 (硬编码 sign) + 移动端 (动态 md5(sha1(...)))
- 信封按域名分: api3.cls.cn → errno; x-quote.cls.cn → code
- tline 的 fields 必须传**字段名** (传数字下标上游静默返回全 null)
- tline 的 data.date 是 **list** 不是 int
- plate/rotation 的 days 只支持 4 / 30, 其它值 data 会变成 dict
- tuyere/stocks 与 popular/stocks 的 change 是**带百分号的字符串**

本插件覆盖 vendor §"测试要求" 的契约测试:
1. 字段映射与单位转换 (股→手 / 小数制不再 /100 / 缺字段置 None 不伪造)
2. symbol 归一与 secu_code 双向转换 (北交所二位前缀 {43,83,87,92})
3. 分钟 datetime 北京墙钟契约
4. 软失败: 单标的失败不阻断整批
5. 能力声明: 只有 daily / minute
6. on_chunk_done 覆盖空批次, 最终 cur == total
"""

__version__ = "1.0.0"

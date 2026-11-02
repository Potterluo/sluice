# 对比结果

模型：`deepseek-v4.1-flash`

| harness | resolved | 平均墙钟 | 平均步数 | 平均 patch | 改测试 |
| --- | --- | --- | --- | --- | --- |
| sluice-v1 | 2/3 | 653s | 80 | 4427B | 0 |
| sluice-v2 | 5/5 | 868s | 83 | 1268B | 0 |
| mini-swe-agent | 5/5 | 227s | 49 | 2610B | 0 |
| mini-agent-harness | 1/5 | 85s | 26 | 1111B | 0 |
| pi | 4/5 | 154s | 30 | 1052B | 0 |

明细：

```json
{
  "sluice-v1": {
    "n": 3,
    "resolved": 2,
    "wall": 652.6999999999999,
    "steps": 80.33333333333333,
    "patch": 4427.333333333333,
    "touched_tests": 0,
    "resolved_ids": [
      "mwaskom__seaborn-3069",
      "pallets__flask-5014"
    ]
  },
  "sluice-v2": {
    "n": 5,
    "resolved": 5,
    "wall": 868.4,
    "steps": 83.0,
    "patch": 1267.6,
    "touched_tests": 0,
    "resolved_ids": [
      "mwaskom__seaborn-3069",
      "mwaskom__seaborn-3187",
      "pallets__flask-5014",
      "pytest-dev__pytest-10051",
      "pytest-dev__pytest-10081"
    ]
  },
  "mini-swe-agent": {
    "n": 5,
    "resolved": 5,
    "wall": 227.4,
    "steps": 49.0,
    "patch": 2610.2,
    "touched_tests": 0,
    "resolved_ids": [
      "mwaskom__seaborn-3069",
      "mwaskom__seaborn-3187",
      "pallets__flask-5014",
      "pytest-dev__pytest-10051",
      "pytest-dev__pytest-10081"
    ]
  },
  "mini-agent-harness": {
    "n": 5,
    "resolved": 1,
    "wall": 85.1,
    "steps": 26.2,
    "patch": 1111.4,
    "touched_tests": 0,
    "resolved_ids": [
      "pallets__flask-5014"
    ]
  },
  "pi": {
    "n": 5,
    "resolved": 4,
    "wall": 153.84,
    "steps": 30.0,
    "patch": 1051.8,
    "touched_tests": 0,
    "resolved_ids": [
      "mwaskom__seaborn-3069",
      "mwaskom__seaborn-3187",
      "pallets__flask-5014",
      "pytest-dev__pytest-10081"
    ]
  }
}
```

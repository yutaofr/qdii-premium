# 可达性探测 PROBE-20260914T231217Z-home-mac

UTC 2026-09-14T23:13:11.059759+00:00

| 来源 | 端点 | 变体 | HTTP | 字节 | RTT ms | 字段完整 | 摘要 |
|---|---|---|---|---|---|---|---|
| sina | etf_batch | no_referer | 403 | 9 | 6357.6 | ❌ | Forbidden |
| sina | etf_batch | referer | 200 | 1335 | 777.0 | ✅ | sh513100:n=34,last=2.191,t=2026-09-14 15:34:59; sz159696:n=34,last=1.999,t=2026-09-14 15:35:00; sz159501:n=34,last=2.083,t=2026-09-14 15:35:30; sz159660:n=34,last=2.321,t=2026-09-14 15:35:45; sh513390:n=34,last=2.389,t=2026-09-14 15:34:59 |
| tencent | etf_batch | default | 200 | 2564 | 1398.0 | ✅ | sh513100:n=88,last=2.191,t=20260914161443; sz159696:n=88,last=1.999,t=20260914161442; sz159501:n=88,last=2.083,t=20260914161433; sz159660:n=88,last=2.321,t=20260914161403; sh513390:n=88,last=2.389,t=20260914161458 |
| eastmoney | stock_get | default | 200 | 172 | 1822.9 | ✅ | f43=2191,f59=3,f60=2201,f86=1789373503 |
| eastmoney | fund_lsjz | referer | 200 | 4481 | 912.5 | ✅ | rows=20,FSRQ=2026-09-11,DWJZ=1.9831,LJJZ=9.9155 |
| sina | hf_NQ | no_referer | 403 | 9 | 5600.2 | ❌ | Forbidden |
| sina | hf_NQ | referer | 200 | 136 | 598.9 | ✅ | n=15,price=29165.800,bid=29167.250,ask=29168.500,time=07:12:43,date=2026-09-15,name=纳斯达克指数期货 |
| sina | fx_spot | no_referer | 403 | 9 | 5597.2 | ❌ | Forbidden |
| sina | fx_spot | referer | 200 | 403 | 596.2 | ✅ | fx_susdcny:n=18,head=02:52:25,6.6980000000,6.7262000000,6.7121000000; fx_susdcnh:n=18,head=07:12:38,6.708500,6.708600,6.708600 |
| cfets | fx_spot_quot | GET | 200 | 2208 | 1497.4 | ❌ | USD/CNY bid=---,ask=---,time=,lastDate=None |
| cfets | fx_spot_quot | POST | 200 | 2208 | 475.5 | ❌ | USD/CNY bid=---,ask=---,time=,lastDate=None |
| sina | ndx_history | default | 200 | 63343 | 2118.8 | ✅ | len=63343,编码负载63323字符（需解码器，未执行） |
| safe | rmb_midrate_page | default | 200 | 16020 | 945.9 | ✅ | len=16020,含“中间价”(utf-8) |
| efunds | fund_page_159696 | default | 200 | 441830 | 3445.0 | ✅ | len=441830,含“159696”(utf-8) |

字段完整只表示结构可解析，不代表数据新鲜、实时或语义已核验。

原始响应与主机信息保存在仓库外 `~/qdii-data/evidence/phase0/reach/20260914T231217Z/`，SHA-256：raw.jsonl `becb14bbe38f9b03498d2208a161b171ad54bf6cf76631e7fb4bfab08025ee43`，host.json `7dabeada1f1330ce669d669e8d7946d14397f631e9c23ebf1307cb9532dfa6d2`。

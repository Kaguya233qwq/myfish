# Myfish

*我的鱼，我做主*

干啥使的：闲鱼的api协议库/sign的python实现

> [!WARNING] 
> 由于本项目的特殊性，不保证稳定可用，并且随时会删库跑路。

### ⚠️警告：在完成对[fireyejs231](https://g.alicdn.com/AWSC/fireyejs/1.231.23/fireyejs.js)的逆向之前，所有的开发工作都是徒劳，因为获取access_token有很大的概率触发 阿里系滑块验证码，需要获取到x5sec的值添加至cookie才能继续访问。

## 已实现功能

✅ 获取新的m_h5_tk

✅ 登陆获取cookie

✅ 获取access_token

## TODO？

- [ ] 补环境实现纯js计算x5sec

- [ ] 完成闲鱼bot事件和消息的抽象封装

- [ ] 增加一些可供外部调用的实用API

# 致谢

感谢 [cv-cat/XianYuApis](https://github.com/cv-cat/XianYuApis) 为本项目提供sign核心算法

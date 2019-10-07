import jieba

seg_list = jieba.cut("我来到北京清华大学，今天天气不错,good day!", cut_all=False)
print("/".join(seg_list))  # 全模式

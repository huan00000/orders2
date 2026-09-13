def closeout():
    '''
    内部每隔24小时持续轮询.当"finished close orders": []数据大于50条时.
    对最旧的第51条数据进行删除
    '''
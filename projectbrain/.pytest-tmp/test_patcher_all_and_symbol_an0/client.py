def get_catalog(tid):
    return call_api(credentials=True, headers=get_headers(tid))

def get_chapter(tid):
    return call_api_v2(credentials=True, headers=get_headers(tid))

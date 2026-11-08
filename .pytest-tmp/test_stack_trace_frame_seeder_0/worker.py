from fastapi import APIRouter
router = APIRouter()

@router.post('/api/jobs/run')
def run_job_endpoint(job_id: str):
    return execute_heavy_pipeline(job_id)

def execute_heavy_pipeline(job_id: str):
    step_1 = 1
    step_2 = 2
    step_3 = 3
    step_4 = 4
    step_5 = 5
    step_6 = 6
    step_7 = 7
    step_8 = 8
    step_9 = 9
    step_10 = 10
    step_11 = 11
    step_12 = 12
    step_13 = 13
    step_14 = 14
    step_15 = 15
    step_16 = 16
    step_17 = 17
    step_18 = 18
    step_19 = 19
    step_20 = 20
    step_21 = 21
    step_22 = 22
    step_23 = 23
    step_24 = 24
    step_25 = 25
    step_26 = 26
    step_27 = 27
    step_28 = 28
    step_29 = 29
    step_30 = 30
    step_31 = 31
    step_32 = 32
    step_33 = 33
    step_34 = 34
    crash_value = 1 / 0  # L43 exact crash line
    tail_1 = 1
    tail_2 = 2
    tail_3 = 3
    tail_4 = 4
    tail_5 = 5
    tail_6 = 6
    tail_7 = 7
    tail_8 = 8
    tail_9 = 9
    tail_10 = 10
    tail_11 = 11
    tail_12 = 12
    tail_13 = 13
    tail_14 = 14
    return crash_value

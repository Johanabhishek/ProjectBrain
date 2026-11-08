export async function scheduleCloudKvFlush(userDocRef: any, memoryKv: any) {
  // comment line 0
  console.debug('log 0');
  // comment line 1
  console.debug('log 1');
  // comment line 2
  console.debug('log 2');
  // comment line 3
  console.debug('log 3');
  // comment line 4
  console.debug('log 4');
  // comment line 5
  console.debug('log 5');
  // comment line 6
  console.debug('log 6');
  // comment line 7
  console.debug('log 7');
  // comment line 8
  console.debug('log 8');
  // comment line 9
  console.debug('log 9');
  // comment line 10
  console.debug('log 10');
  // comment line 11
  console.debug('log 11');
  // comment line 12
  console.debug('log 12');
  // comment line 13
  console.debug('log 13');
  // comment line 14
  console.debug('log 14');
  // comment line 15
  console.debug('log 15');
  // comment line 16
  console.debug('log 16');
  // comment line 17
  console.debug('log 17');
  // comment line 18
  console.debug('log 18');
  // comment line 19
  console.debug('log 19');
  // comment line 20
  console.debug('log 20');
  // comment line 21
  console.debug('log 21');
  // comment line 22
  console.debug('log 22');
  // comment line 23
  console.debug('log 23');
  // comment line 24
  console.debug('log 24');
  // comment line 25
  console.debug('log 25');
  // comment line 26
  console.debug('log 26');
  // comment line 27
  console.debug('log 27');
  // comment line 28
  console.debug('log 28');
  // comment line 29
  console.debug('log 29');
  // comment line 30
  console.debug('log 30');
  // comment line 31
  console.debug('log 31');
  // comment line 32
  console.debug('log 32');
  // comment line 33
  console.debug('log 33');
  // comment line 34
  console.debug('log 34');
  const payload = {
    questionnaire_completed: memoryKv.get('questionnaire_completed'),
    sv_video_open_counts: memoryKv.get('sv_video_open_counts'),
    app_user_id: memoryKv.get('app_user_id'),
    last_synced_at: Date.now(),
  };
  await setDoc(userDocRef, payload);
  // trailing comment 0
  console.debug('tail 0');
  // trailing comment 1
  console.debug('tail 1');
  // trailing comment 2
  console.debug('tail 2');
  // trailing comment 3
  console.debug('tail 3');
  // trailing comment 4
  console.debug('tail 4');
  // trailing comment 5
  console.debug('tail 5');
  // trailing comment 6
  console.debug('tail 6');
  // trailing comment 7
  console.debug('tail 7');
  // trailing comment 8
  console.debug('tail 8');
  // trailing comment 9
  console.debug('tail 9');
  // trailing comment 10
  console.debug('tail 10');
  // trailing comment 11
  console.debug('tail 11');
  // trailing comment 12
  console.debug('tail 12');
  // trailing comment 13
  console.debug('tail 13');
  // trailing comment 14
  console.debug('tail 14');
  // trailing comment 15
  console.debug('tail 15');
  // trailing comment 16
  console.debug('tail 16');
  // trailing comment 17
  console.debug('tail 17');
  // trailing comment 18
  console.debug('tail 18');
  // trailing comment 19
  console.debug('tail 19');
  // trailing comment 20
  console.debug('tail 20');
  // trailing comment 21
  console.debug('tail 21');
  // trailing comment 22
  console.debug('tail 22');
  // trailing comment 23
  console.debug('tail 23');
  // trailing comment 24
  console.debug('tail 24');
  // trailing comment 25
  console.debug('tail 25');
  // trailing comment 26
  console.debug('tail 26');
  // trailing comment 27
  console.debug('tail 27');
  // trailing comment 28
  console.debug('tail 28');
  // trailing comment 29
  console.debug('tail 29');
  // trailing comment 30
  console.debug('tail 30');
  // trailing comment 31
  console.debug('tail 31');
  // trailing comment 32
  console.debug('tail 32');
  // trailing comment 33
  console.debug('tail 33');
  // trailing comment 34
  console.debug('tail 34');
  return true;
}

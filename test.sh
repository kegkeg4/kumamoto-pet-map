#!/bin/bash
B=http://127.0.0.1:8000
J(){ python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$1',''))"; }
echo "== 1) 迷子猫を登録 =="
R1=$(curl -s -X POST $B/api/pets -d '{"kind":"lost","species":"cat","name":"タマ","breed":"キジトラ","color":"茶縞","features":"しっぽ先が白い","event_at":"2026-07-28T17:00","lat":32.803,"lng":130.708,"address":"中央区新市街","collar":true}')
CID=$(echo $R1 | J id); CTOK=$(echo $R1 | J admin_token)
echo "id=$CID"
echo "== 2) 迷子犬を登録 =="
R2=$(curl -s -X POST $B/api/pets -d '{"kind":"lost","species":"dog","name":"モカ","size":"small","color":"茶","event_at":"2026-07-28T18:00","lat":32.81,"lng":130.71}')
DID=$(echo $R2 | J id)
echo "id=$DID"
echo "== 3) 保護情報(連絡先なし→拒否) =="
curl -s -X POST $B/api/pets -d '{"kind":"found","species":"cat","color":"茶縞","event_at":"2026-07-29T09:00","lat":32.804,"lng":130.709}'
echo; echo "== 4) 保護情報(正常・キジトラ猫) =="
R4=$(curl -s -X POST $B/api/pets -d '{"kind":"found","species":"cat","breed":"キジトラ?","color":"茶縞","features":"人慣れしている","event_at":"2026-07-29T09:00","lat":32.804,"lng":130.709,"address":"新市街アーケード付近","contact":"090-1111-2222","shelter_info":"自宅で保護中"}')
FID=$(echo $R4 | J id)
echo "id=$FID status=$(curl -s $B/api/pets/$FID | J status)"
echo "== 5) ハニーポット(スパム) =="
curl -s -X POST $B/api/pets -d '{"kind":"lost","species":"dog","name":"spam","event_at":"2026-07-28T18:00","lat":32.8,"lng":130.7,"website":"http://spam"}' | J id > /dev/null && echo "偽成功レス返却OK"
echo "== 6) 一覧: kind別・species別 =="
echo "  lost全体: $(curl -s "$B/api/pets?kind=lost" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['pets']))")件"
echo "  lost猫のみ: $(curl -s "$B/api/pets?kind=lost&species=cat" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['pets']))")件"
echo "  found: $(curl -s "$B/api/pets?kind=found" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['pets']))")件"
echo "== 7) stats =="
curl -s $B/api/stats
echo; echo "== 8) 迷子猫詳細にsimilar(保護キジトラ)が出る =="
curl -s $B/api/pets/$CID | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('  similar:', [(s['kind'],s['breed']) for s in d['similar']])"
echo "== 9) 保護詳細にsimilar(迷子猫タマ)が出る =="
curl -s $B/api/pets/$FID | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('  similar:', [(s['kind'],s['name']) for s in d['similar']])"
echo "== 10) 目撃投稿+塗り+更新 =="
curl -s -X POST $B/api/pets/$CID/sightings -d '{"lat":32.8032,"lng":130.7082,"seen_at":"2026-07-30T22:10","memo":"駐車場の車の下"}' > /dev/null
curl -s -X POST $B/api/pets/$CID/searched -d '{"cells":["24298_80684","24298_80685"]}' > /dev/null
curl -s -X POST "$B/api/pets/$CID/updates?token=$CTOK" -d '{"body":"夜間に捜索します"}' > /dev/null
curl -s $B/api/pets/$CID | python3 -c "import json,sys;d=json.load(sys.stdin);print('  目撃',len(d['sightings']),'/ セル',len(d['searched']),'/ 更新',len(d['updates']))"
echo "== 11) 誤トークンで更新→403 =="
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$B/api/pets/$CID/updates?token=wrong" -d '{"body":"x"}'
echo "== 12) 生成(猫用テンプレ・追いかけない文言) =="
curl -s -X POST $B/api/generate -d "{\"pet_id\":\"$CID\"}" | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('  x:', d['x'][:44]); print('  note:', d['flyer_note'])"
echo "== 13) 保護→再会ステータス変更 =="
FTOK=$(echo $R4 | J admin_token)
curl -s -X PATCH "$B/api/pets/$FID?token=$FTOK" -d '{"status":"reunited"}'
echo; echo "== 14) 保護に不正status(searching)を拒否 =="
curl -s -X PATCH "$B/api/pets/$FID?token=$FTOK" -d '{"status":"searching"}' > /dev/null
curl -s $B/api/pets/$FID | J status
echo "== 15) 県外座標拒否 =="
curl -s -X POST $B/api/pets -d '{"kind":"lost","species":"dog","name":"東京犬","event_at":"2026-07-28T18:00","lat":35.68,"lng":139.76}'
echo; echo "== 16) 通報3件で自動非表示 =="
for i in 1 2 3; do curl -s -X POST $B/api/pets/$DID/flag -d '{}' > /dev/null; done
curl -s -o /dev/null -w '%{http_code}\n' $B/api/pets/$DID
echo "== 17) 静的配信+アップロード検証 =="
curl -s -o /dev/null -w "index: %{http_code} / " localhost:8000/
PNG="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
curl -s -X POST $B/api/upload -d "{\"data\":\"$PNG\"}" | J file

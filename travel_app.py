"""여행 지갑 | 실행: streamlit run travel_app.py
GitHub에 이 파일과 requirements.txt를 올린 후 Streamlit Community Cloud에 배포.
앱 Settings > Secrets에 다음 TOML 입력 (실제 비밀값은 GitHub에 올리지 않기):

[trip]
title = "우리의 여행"
password = "충분히-긴-여행공유비밀번호"
members = ["민수", "지영", "수진"]
app_url = "https://YOUR-APP.streamlit.app"

[supabase]
url = "https://YOUR-PROJECT.supabase.co"
service_role_key = "YOUR-SERVER-ONLY-KEY"

실제 사용 시 Supabase SQL Editor에서 아래 SQL을 실행하고 위 Secrets 연결:
create table public.travel_expenses (
 id text primary key,
 payer text not null,
 amount bigint not null check (amount > 0),
 beneficiaries jsonb not null,
 memo text not null,
 created_at timestamptz not null default now()
);
alter table public.travel_expenses enable row level security;
revoke all on public.travel_expenses from anon, authenticated;

앱 하나당 여행 하나. 여행 중 members를 변경하지 마세요.
비밀번호를 아는 사람 모두가 조회/입력/삭제할 수 있는 공동 장부입니다.
서비스 역할 키는 서버 Secrets 전용입니다. 클라이언트/공개 저장소에 넣지 마세요.
Supabase 설정이 없으면 SQLite 체험 모드(재시작·재배포 시 데이터 소실 가능).
원화 정수 균등 분담만 지원. 나머지 원은 이름순으로 1원씩 배분합니다.
송금 제안은 잔액을 맞추는 계산이며 최소 송금 횟수를 보장하지 않습니다.
실제 계좌 이체나 이체 완료 추적 기능은 없습니다.
"""
import csv
import hmac
import html
import io
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import qrcode
import requests
import streamlit as st

st.set_page_config(page_title="여행 지갑", page_icon="🧳", layout="centered", initial_sidebar_state="collapsed")

def settings(key):
    try:
        return dict(st.secrets.get(key, {}))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return {}

trip, sb = settings('trip'), settings('supabase')
members_raw = trip.get('members', ['나', '친구'])
members = [str(x).strip() for x in members_raw] if isinstance(members_raw, list) else []
remote = bool(sb.get('url') and sb.get('service_role_key'))
st.markdown('''<style>
#MainMenu,footer{visibility:hidden} header{background:transparent!important}
.block-container{max-width:500px;padding:.8rem .85rem .4rem!important}
[data-testid="stAppViewContainer"]{background:#f5faf8}
[data-testid="stVerticalBlock"]{gap:.4rem}
h1{font-size:1.5rem!important;padding:0!important;letter-spacing:-.6px}
[data-testid="stForm"]{padding:.65rem;border-radius:16px;background:white}
.stButton button,.stFormSubmitButton button{border-radius:12px;min-height:36px}
input,textarea{font-size:16px!important}
.stats{background:#e0f1e9;border-radius:14px;padding:10px 12px;font-size:14px}
.record{background:white;border:1px solid #dde9e2;border-radius:12px;padding:10px;font-size:14px;overflow-wrap:anywhere}
</style>''', unsafe_allow_html=True)
st.title('🧳 여행 지갑')
st.caption(str(trip.get('title', '함께 쓰고, 깔끔하게 나누기')))
if len(members) < 2 or len(members) > 12 or len(set(members)) != len(members) or not all(members):
    st.error('Secrets의 trip.members에 서로 다른 이름을 2~12명 입력해주세요.')
    st.stop()
if not trip.get('password'):
    st.info('앱 Settings → Secrets에 trip.password를 설정해주세요. 설정 예시와 저장소 SQL은 파일 맨 위에 있습니다.')
    st.stop()
if bool(sb.get('url')) != bool(sb.get('service_role_key')):
    st.error('Supabase url과 service_role_key를 모두 설정해주세요.')
    st.stop()
if not st.session_state.get('authenticated'):
    with st.form('login'):
        password = st.text_input('여행 공유 비밀번호', type='password')
        login = st.form_submit_button('여행 장부 열기', use_container_width=True)
    if login:
        now = time.time()
        if now < st.session_state.get('retry_after', 0):
            st.error('잠시 후 다시 시도해주세요.')
        elif hmac.compare_digest(password.encode(), str(trip['password']).encode()):
            st.session_state.authenticated = True
            st.rerun()
        else:
            n = st.session_state.get('failures', 0) + 1
            st.session_state.failures = n
            st.session_state.retry_after = now + min(60, 2 ** min(n, 6))
            st.error('비밀번호를 확인해주세요.')
    st.caption('비밀번호를 아는 여행 멤버가 함께 관리하는 공동 장부예요.')
    st.stop()

def db():
    con = sqlite3.connect(str(Path(__file__).with_name('travel_demo.sqlite3')), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS travel_expenses (id TEXT PRIMARY KEY,payer TEXT NOT NULL,amount INTEGER NOT NULL,beneficiaries TEXT NOT NULL,memo TEXT NOT NULL,created_at TEXT NOT NULL)')
    return con

def api(method, params=None, payload=None, duplicate=False):
    key = str(sb['service_role_key'])
    r = requests.request(method, str(sb['url']).rstrip('/') + '/rest/v1/travel_expenses',
        params=params, json=payload, timeout=15,
        headers={'apikey': key, 'Authorization': f'Bearer {key}',
                 'Prefer': 'resolution=ignore-duplicates,return=minimal' if duplicate else 'return=minimal'})
    r.raise_for_status()
    return r.json() if r.content else None

def entries():
    if remote:
        result, offset = [], 0
        while True:
            page = api('GET', {'select': '*', 'order': 'created_at.asc,id.asc', 'limit': '1000', 'offset': str(offset)})
            result.extend(page)
            if not page:
                break
            offset += len(page)
        return result
    with db() as con:
        result = [dict(r) for r in con.execute('SELECT * FROM travel_expenses ORDER BY created_at,id')]
    for r in result:
        r['beneficiaries'] = json.loads(r['beneficiaries'])
    return result

def save(row):
    if remote:
        api('POST', payload=row, duplicate=True)
    else:
        with db() as con:
            con.execute('INSERT OR IGNORE INTO travel_expenses VALUES (?,?,?,?,?,?)',
                (row['id'], row['payer'], row['amount'], json.dumps(row['beneficiaries'], ensure_ascii=False), row['memo'], row['created_at']))

def delete(entry_id):
    if remote:
        api('DELETE', {'id': f'eq.{entry_id}'})
    else:
        with db() as con:
            con.execute('DELETE FROM travel_expenses WHERE id=?', (entry_id,))

def balances(rows, people):
    paid = {p: 0 for p in people}
    owed = {p: 0 for p in people}
    for r in rows:
        targets = sorted(r['beneficiaries'])
        if r['payer'] not in paid or not targets or len(targets) != len(set(targets)) or any(p not in paid for p in targets) or int(r['amount']) <= 0:
            raise ValueError('저장된 멤버/금액이 현재 설정과 맞지 않습니다. 여행 중에는 멤버를 변경하지 마세요.')
        amount = int(r['amount'])
        paid[r['payer']] += amount
        share, remainder = divmod(amount, len(targets))
        for i, p in enumerate(targets):
            owed[p] += share + (i < remainder)
    return paid, owed, {p: paid[p] - owed[p] for p in people}

def transfers(net):
    debt = [[p, -v] for p, v in sorted(net.items()) if v < 0]
    credit = [[p, v] for p, v in sorted(net.items()) if v > 0]
    result, i, j = [], 0, 0
    while i < len(debt) and j < len(credit):
        value = min(debt[i][1], credit[j][1])
        result.append((debt[i][0], credit[j][0], value))
        debt[i][1] -= value
        credit[j][1] -= value
        if debt[i][1] == 0: i += 1
        if credit[j][1] == 0: j += 1
    return result

a,b,c = st.columns([5,1,1])
a.caption('공동 장부 · ' + ('클라우드 저장' if remote else '⚠️ 임시 체험 저장'))
if b.button('↻', help='최신 지출과 정산 다시 계산', use_container_width=True): st.rerun()
with c.popover('⚙️', use_container_width=True):
    url = str(trip.get('app_url', '')).strip()
    if url.startswith('https://'):
        buf = io.BytesIO(); qrcode.make(url).save(buf, format='PNG')
        st.image(buf.getvalue(), width=180)
        st.link_button('여행 앱 열기', url)
        st.caption('QR에는 주소만 들어갑니다. 비밀번호는 따로 전달하세요.')
    else:
        st.caption('trip.app_url에 배포 주소를 넣으면 공유 QR이 표시됩니다.')
    st.caption('원화 · 균등 분담 · 나머지 원은 이름순 배분')
    if not remote: st.warning('재시작 시 데이터가 사라질 수 있어요. 실제 사용은 Supabase를 연결하세요.')
    if st.button('로그아웃'):
        st.session_state.clear(); st.rerun()

# 폼 밖 입력은 실패해도 유지됩니다. 저장이 성공한 경우에만 콜백에서 초기화.
def submit():
    memo = st.session_state.get('memo', '').strip()
    targets = st.session_state.get('targets', [])
    amount = int(st.session_state.get('amount', 0))
    if not memo or not targets or amount <= 0:
        st.session_state.notice = ('warning', '내용·금액·분담할 사람을 모두 입력해주세요.')
        return
    row = dict(id=st.session_state.draft_id, payer=st.session_state.payer, amount=amount,
        beneficiaries=sorted(targets), memo=memo, created_at=datetime.now(timezone.utc).isoformat())
    # 동일 요청 재전송은 같은 UUID로 처리. 실패 시에는 재시도/조회 전 입력을 바꾸지 않도록 보관.
    row = st.session_state.get('pending', row)
    st.session_state.pending = row
    try:
        save(row)
    except (requests.RequestException, sqlite3.Error):
        st.session_state.notice = ('error', '저장 결과를 확인하지 못했어요. 입력을 유지했습니다. ↻로 목록 확인 후 같은 저장 버튼으로 재시도하세요.')
        return
    st.session_state.pop('pending', None)
    st.session_state.draft_id = str(uuid.uuid4())
    st.session_state.memo = ''
    st.session_state.amount = 0
    st.session_state.notice = ('success', '지출을 기록했어요!')

st.session_state.setdefault('draft_id', str(uuid.uuid4()))
with st.form('expense'):
    left, right = st.columns([1,1.2])
    left.selectbox('결제한 사람', members, key='payer')
    right.number_input('금액 (원)', min_value=0, max_value=1000000000, step=1000, key='amount')
    st.text_input('사용 내용', placeholder='예: 저녁 식사', max_chars=80, key='memo')
    st.multiselect('함께 나눌 사람', members, default=members, key='targets')
    st.form_submit_button('지출 기록 +', on_click=submit, type='primary', use_container_width=True)
if 'notice' in st.session_state:
    level, text = st.session_state.pop('notice')
    getattr(st, level)(text)
if st.session_state.get('pending'):
    st.caption('미확정 저장이 있어요. 재시도하면 최초 요청 내용으로 저장됩니다. 입력을 바꾸지 마세요.')

try:
    rows = entries()
    paid, owed, net = balances(rows, members)
except (requests.RequestException, sqlite3.Error, ValueError, KeyError, TypeError):
    st.error('장부를 불러오지 못했어요. 저장소 연결과 멤버 설정을 확인하고 ↻를 눌러주세요.')
    st.stop()

total = sum(int(r['amount']) for r in rows)
st.markdown(f'<div class="stats">총 지출 <b>{total:,}원</b> · {len(rows)}건 · {len(members)}명</div>', unsafe_allow_html=True)
view = st.segmented_control('장부 보기', ['정산', '내역'], default='정산', label_visibility='collapsed')
# 고정 높이 내부 스크롤로 장부 전체가 길어져도 페이지 길이를 억제합니다.
with st.container(height=190, border=False):
    if view == '내역':
        if not rows:
            st.caption('첫 지출을 기록해보세요 🌿')
        else:
            lookup = {r['id']: r for r in reversed(rows)}
            selected = st.selectbox('지출 선택', list(lookup), format_func=lambda k: f"{lookup[k]['memo']} · {int(lookup[k]['amount']):,}원 · {lookup[k]['payer']}")
            r = lookup[selected]
            stamp = datetime.fromisoformat(r['created_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M')
            st.caption(f"{stamp} · 분담: {', '.join(r['beneficiaries'])}")
            with st.popover('이 지출 삭제'):
                st.warning('공동 장부에서 영구 삭제됩니다.')
                confirm = st.checkbox('삭제할 지출을 확인했어요', key='delete_confirm_' + selected)
                if st.button('영구 삭제', disabled=not confirm, key='delete_' + selected):
                    try:
                        delete(selected); st.rerun()
                    except (requests.RequestException, sqlite3.Error):
                        st.error('삭제하지 못했어요. 다시 조회해주세요.')
    else:
        suggested = transfers(net)
        if suggested:
            for sender, receiver, value in suggested:
                st.markdown(f"**{sender} → {receiver} · {value:,}원**")
        else:
            st.caption('지출이 없거나 모두 정산이 맞아요 ✨')
        with st.expander('개인별 결제 · 부담 · 받을/낼 금액'):
            for p in members:
                direction = '받을 돈' if net[p] >= 0 else '낼 돈'
                st.caption(f'{p} | 결제 {paid[p]:,} · 부담 {owed[p]:,} · {direction} {abs(net[p]):,}원')
        st.caption('제안대로 전부 송금하면 정산 완료 · 실제 이체/완료 체크는 지원하지 않아요.')

with st.popover('CSV 백업'):
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(['일시(UTC)', '내용', '결제자', '금액(원)', '분담자'])
    def safe_cell(x):
        value = str(x)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
    for r in rows:
        writer.writerow([safe_cell(r['created_at']), safe_cell(r['memo']), safe_cell(r['payer']), int(r['amount']), safe_cell(', '.join(r['beneficiaries']))])
    st.download_button('현재 장부 다운로드', out.getvalue().encode('utf-8-sig'), 'travel_expenses.csv', 'text/csv', use_container_width=True)
    st.caption('CSV는 내려받기 전용이며 가져오기 기능은 없습니다.')

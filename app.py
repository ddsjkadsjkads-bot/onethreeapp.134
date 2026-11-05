"""생활 루틴 도우미
실행: streamlit run app.py
GitHub에 app.py, requirements.txt를 업로드하고 Streamlit Community Cloud에서 배포.
데이터는 세션에 보관됨. 종료/새로고침 전 JSON 백업 필수. 영구 저장/기기 동기화 없음.
알림은 앱이 활성 상태로 연결된 동안만 동작. 잠금 화면 푸시/복약 안전 판단 미지원.
"""
from __future__ import annotations
import io
import json
import uuid
from datetime import datetime, timedelta, timezone, date, time
from urllib.parse import urlparse
import streamlit as st
import qrcode
from PIL import Image, ImageDraw

KST = timezone(timedelta(hours=9))
CATEGORIES = ['💊 약 먹기', '📚 공부·숙제', '🧹 청소·집안일', '🏃 건강·운동', '📝 기타']
REPEATS = {'한 번만': 0, '매일': 1, '매주': 7}
PRIORITIES = {1: '1 · 꼭 해야 함', 2: '2 · 오늘 하면 좋음', 3: '3 · 여유 있을 때'}


def now_kst():
    return datetime.now(KST)


def new_task(title, category, due, minutes=10, priority=2, repeat=0):
    return {'id': uuid.uuid4().hex, 'title': title.strip(), 'category': category,
            'due': due.isoformat(), 'minutes': int(minutes), 'priority': int(priority),
            'repeat': int(repeat), 'snooze': None, 'notified': [], 'history': []}


def effective_due(task):
    return datetime.fromisoformat(task['snooze'] or task['due'])


def candidates(tasks, now, available):
    # 이미 오늘 완료한 반복 일정 등 미래 날짜의 일은 추천에서 제외한다.
    eligible = [t for t in tasks if effective_due(t).astimezone(KST).date() <= now.date()
                and t['minutes'] <= available]
    return sorted(eligible, key=lambda t: (effective_due(t) > now, t['priority'], effective_due(t)))


def finish_task(task, now, status='완료'):
    task['history'].append({'at': now.isoformat(), 'scheduled': task['due'], 'status': status})
    if task['repeat'] == 0:
        return False
    # 완료 시각이 아닌 원래 예정 시각을 기준으로 다음 회차 생성.
    due = datetime.fromisoformat(task['due'])
    step = timedelta(days=task['repeat'])
    while due <= now:
        due += step
    if due.isoformat() == task['due']:
        due += step
    task.update(due=due.isoformat(), snooze=None, notified=[])
    return True


def validate_backup(raw):
    if len(raw) > 2_000_000:
        raise ValueError('2MB 이하의 백업만 지원합니다.')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('app') != 'daily-routine' or data.get('version') != 1:
        raise ValueError('이 생활 루틴 앱의 백업 파일을 선택하세요.')
    seen = set()
    for group in ['tasks', 'archive']:
        rows = data.get(group)
        if not isinstance(rows, list) or len(rows) > 2000:
            raise ValueError('목록 형식이 올바르지 않습니다.')
        for t in rows:
            if not isinstance(t, dict):
                raise ValueError('잘못된 일정입니다.')
            ident = t.get('id')
            if not isinstance(ident, str) or not ident or len(ident) > 80 or ident in seen:
                raise ValueError('일정 식별자가 올바르지 않습니다.')
            seen.add(ident)
            if not isinstance(t.get('title'), str) or not t['title'].strip() or len(t['title']) > 100:
                raise ValueError('일정 제목이 올바르지 않습니다.')
            if t.get('category') not in CATEGORIES:
                raise ValueError('분류가 올바르지 않습니다.')
            for key, allowed in [('priority', (1, 2, 3)), ('repeat', (0, 1, 7))]:
                if type(t.get(key)) is not int or t[key] not in allowed:
                    raise ValueError('중요도 또는 반복 설정이 올바르지 않습니다.')
            if type(t.get('minutes')) is not int or not 1 <= t['minutes'] <= 480:
                raise ValueError('소요 시간이 올바르지 않습니다.')
            for key in ['due', 'snooze']:
                v = t.get(key)
                if key == 'snooze' and v is None:
                    continue
                if not isinstance(v, str) or datetime.fromisoformat(v).utcoffset() is None:
                    raise ValueError('시간대가 포함된 날짜가 필요합니다.')
            if not isinstance(t.get('notified'), list) or any(v not in ['before', 'due'] for v in t['notified']):
                raise ValueError('알림 상태가 올바르지 않습니다.')
            if not isinstance(t.get('history'), list) or len(t['history']) > 10000:
                raise ValueError('완료 기록이 올바르지 않습니다.')
            for h in t['history']:
                if not isinstance(h, dict) or h.get('status') not in ['완료', '건너뜀']:
                    raise ValueError('기록 형식이 올바르지 않습니다.')
                for key in ['at', 'scheduled']:
                    if not isinstance(h.get(key), str) or datetime.fromisoformat(h[key]).utcoffset() is None:
                        raise ValueError('기록 시간이 올바르지 않습니다.')
    return data


@st.fragment(run_every='5s')
def alerts():
    now = now_kst()
    messages = []
    for task in st.session_state['tasks']:
        due = effective_due(task)
        seconds = (due - now).total_seconds()
        stage = 'due' if seconds <= 0 else 'before' if seconds <= 300 else None
        if not stage:
            continue
        text = ('예정 시각 도착·경과' if stage == 'due' else '5분 이내 시작') + f" · {task['title']} ({due.astimezone(KST):%m/%d %H:%M})"
        messages.append(text)
        if stage not in task['notified']:
            st.toast(text, icon='⏰')
            task['notified'].append(stage)
    if messages:
        with st.expander(f'⏰ 확인할 알림 {len(messages)}개', expanded=False):
            for msg in messages:
                st.warning(msg)


def character_image(happy=False):
    """외부 파일 없이 그리는 오리지널 새싹 구름 캐릭터 몽실이."""
    im = Image.new('RGBA', (300, 230), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    outline = '#675b70'
    d.ellipse((60, 197, 242, 218), fill='#e6dfef')
    d.ellipse((73, 165, 123, 209), fill='#fff5ec', outline=outline, width=4)
    d.ellipse((185, 165, 229, 209), fill='#fff5ec', outline=outline, width=4)
    d.rounded_rectangle((46, 55, 258, 195), radius=65, fill='#fff5ec', outline=outline, width=5)
    d.ellipse((109, 25, 157, 64), fill='#c4e5bc', outline=outline, width=3)
    d.ellipse((149, 15, 195, 58), fill='#a9d9bf', outline=outline, width=3)
    d.line((151, 60, 151, 39), fill=outline, width=4)
    d.ellipse((75, 125, 107, 143), fill='#f5becb')
    d.ellipse((199, 125, 231, 143), fill='#f5becb')
    if happy:
        d.arc((109, 103, 131, 126), 190, 350, fill=outline, width=4)
        d.arc((173, 103, 195, 126), 190, 350, fill=outline, width=4)
        d.arc((139, 123, 168, 147), 0, 180, fill=outline, width=4)
    else:
        d.ellipse((117, 110, 126, 124), fill=outline)
        d.ellipse((178, 110, 187, 124), fill=outline)
        d.arc((141, 125, 165, 141), 0, 180, fill=outline, width=3)
    buffer = io.BytesIO()
    im.save(buffer, format='PNG')
    return buffer.getvalue()


def companion_panel(rows, now):
    # 보상을 위해 약을 여러 번 복용하도록 유도하지 않는다.
    # 모든 분류에서 같은 일정의 같은 날짜 완료는 한 번만 보상한다.
    days = set()
    rewards = set()
    for task in rows:
        for h in task['history']:
            if h['status'] == '완료':
                day = datetime.fromisoformat(h['at']).astimezone(KST).date()
                days.add(day)
                # 약 카테고리는 일정 개수와 관계없이 하루 보상 한 번만.
                key = 'medicine' if task['category'] == CATEGORIES[0] else task['id']
                rewards.add((key, day))
    stars = len(rewards)
    level = stars // 5 + 1
    today_done = now.date() in days
    cursor = now.date() if today_done else now.date() - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    left, right = st.columns([1, 2])
    left.image(character_image(today_done), width=115)
    with right:
        st.markdown(f'**몽실이 · Lv.{level}**  🌟 {stars} 별사탕')
        st.caption('오늘도 한 걸음 해냈네! 함께 쉬어도 좋아.' if today_done else '작은 일 하나부터 같이 해볼까? 늦어도 괜찮아.')
        st.progress((stars % 5) / 5, text=f'다음 레벨까지 {5 - stars % 5}개 · 연속 실천 {streak}일')
    with st.expander('🌱 몽실이와 함께하는 방법'):
        st.caption('완료하면 별사탕 1개, 5개마다 레벨 업! 같은 일정은 하루 한 번만 보상합니다. 약 먹기는 모든 약 일정을 합쳐 하루 한 번만 보상합니다. 건너뛰기에는 보상이 없고, 미완료에 벌점도 없어요. 별사탕은 완료 기록에서 계산되므로 기록을 삭제하면 줄어듭니다. 약은 반드시 처방대로만 복용하세요.')


def main():
    st.set_page_config(page_title='오늘 한 걸음', page_icon='🌱', layout='centered')
    st.markdown('''<style>
    .stApp{background:#f5f7fb}.block-container{max-width:560px;padding:.65rem .8rem 1rem}
    [data-testid="stVerticalBlock"]{gap:.45rem}[data-testid="stHeader"]{height:0}
    .stButton button,.stFormSubmitButton button{border-radius:12px;min-height:2.5rem}
    [data-testid="stForm"]{background:white;border-radius:16px;padding:.65rem}
    @media(max-width:640px){[data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:.4rem}
    [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{min-width:0!important;flex:1 1 0!important}}
    </style>''', unsafe_allow_html=True)
    for k in ['tasks', 'archive']:
        if k not in st.session_state:
            st.session_state[k] = []
    tasks, archive = st.session_state['tasks'], st.session_state['archive']
    now = now_kst()
    a,b = st.columns([4,1])
    a.markdown('### 🌱 오늘 한 걸음')
    with b.popover('⚙️'):
        st.write('**QR · 데이터 보관**')
        url = st.text_input('배포된 앱 HTTPS 주소')
        if url:
            u = urlparse(url.strip())
            if u.scheme == 'https' and u.netloc and not u.username and not u.password:
                buffer = io.BytesIO(); qrcode.make(url.strip()).save(buffer, format='PNG')
                st.image(buffer.getvalue(), width=180)
                st.download_button('QR 저장', buffer.getvalue(), 'routine-qr.png', 'image/png')
            else:
                st.warning('HTTPS 앱 주소를 입력하세요.')
        payload = json.dumps({'app':'daily-routine','version':1,'tasks':tasks,'archive':archive}, ensure_ascii=False, indent=2)
        st.download_button('일정·기록 백업', payload, 'routine-backup.json', 'application/json')
        upload = st.file_uploader('백업 복원', type=['json'])
        confirm = st.checkbox('현재 일정과 기록을 백업으로 교체')
        if st.button('복원', disabled=upload is None or not confirm):
            try:
                data = validate_backup(upload.getvalue())
            except (ValueError, TypeError, KeyError, OverflowError) as e:
                st.error(f'복원 실패: {e}')
            else:
                st.session_state['tasks'], st.session_state['archive'] = data['tasks'], data['archive']
                st.rerun()
        st.caption('데이터는 세션에만 보관됩니다. 새로고침·연결 종료 전 백업하세요. QR은 앱 주소만 열며 다른 기기와 자동 공유되지 않습니다.')
    st.caption('할 일을 적는 데서 끝내지 않고, 지금 할 일과 다음 회차까지 챙겨요.')
    st.caption('알림: 앱이 활성 상태로 연결된 동안 약 5초 간격 · 한국 시간 · 잠금 화면 푸시 없음')
    alerts()
    all_rows = tasks + archive
    companion_panel(all_rows, now)
    done_today = sum(h['status']=='완료' and datetime.fromisoformat(h['at']).astimezone(KST).date()==now.date() for t in all_rows for h in t['history'])
    due_today = [t for t in tasks if effective_due(t).astimezone(KST).date() <= now.date()]
    st.markdown(f'**오늘 완료 {done_today}회** · 남은 일 {len(due_today)}개 · 예상 {sum(t["minutes"] for t in due_today)}분')

    # 달력·여러 페이지 대신 한 화면의 접이식 도구와 추천 카드 사용.
    with st.expander('＋ 할 일 추가 · 빠른 시작', expanded=not bool(tasks or archive)):
        quick = st.selectbox('빠른 입력 템플릿', ['직접 입력', '약 먹기', '숙제하기', '방 청소'])
        defaults = {'직접 입력':('',4,10,2,0), '약 먹기':('약 먹기',0,1,1,1), '숙제하기':('숙제하기',1,30,1,0), '방 청소':('방 청소',2,15,2,7)}
        title, cat, duration, pri, repeat = defaults[quick]
        with st.form(f'add_{quick}', clear_on_submit=True):
            title_in = st.text_input('할 일', value=title, max_chars=100)
            c1,c2 = st.columns(2)
            category = c1.selectbox('분류', CATEGORIES, index=cat)
            priority = c2.selectbox('중요도', [1,2,3], index=pri-1, format_func=lambda x:PRIORITIES[x])
            c1,c2 = st.columns(2)
            day = c1.date_input('날짜', value=now.date())
            at = c2.time_input('시각', value=(now+timedelta(minutes=10)).time().replace(second=0,microsecond=0), step=60)
            c1,c2 = st.columns(2)
            minutes = c1.number_input('예상 소요(분)', 1, 480, duration)
            cadence = c2.selectbox('반복', list(REPEATS), index=list(REPEATS.values()).index(repeat))
            if st.form_submit_button('일정 만들기', type='primary', use_container_width=True):
                if title_in.strip():
                    tasks.append(new_task(title_in,category,datetime.combine(day,at,tzinfo=KST),minutes,priority,REPEATS[cadence]))
                    st.rerun()
                else:
                    st.warning('할 일을 입력하세요.')
        st.caption('약 일정은 처방·복용 안내에 맞춰 직접 지정하세요. 이 앱은 복용 여부를 판단하거나 추가 복용을 권하지 않습니다.')

    available = st.select_slider('지금 쓸 수 있는 시간', options=[5,10,15,30,60,120,480], value=30, format_func=lambda n:f'{n}분')
    matches = candidates(tasks, now, available)
    st.markdown('**🎯 지금 할 일 추천**')
    if matches:
        best = matches[0]
        due = effective_due(best).astimezone(KST)
        st.info(f"{best['title']} · {best['minutes']}분\n\n{PRIORITIES[best['priority']]} · {'예정 시각 경과' if due<=now else '오늘 예정'} · {due:%H:%M}")
        if st.button('이 일 바로 선택', use_container_width=True):
            st.session_state['selected_task'] = best['id']
    elif due_today:
        st.success('이 시간 안에 끝낼 일은 없어요. 여유 시간을 늘리거나 아래에서 직접 선택하세요.')
    else:
        st.success('오늘 남은 일정이 없어요. 내일 일정은 아래에서 확인할 수 있어요.')

    st.markdown('**☑️ 전체 할 일 · 시간순**')
    if tasks:
        ordered = sorted(tasks, key=effective_due)
        mapping = {t['id']:t for t in ordered}
        if st.session_state.get('selected_task') not in mapping:
            st.session_state['selected_task'] = ordered[0]['id']
        ident = st.selectbox('실행할 일', list(mapping), key='selected_task', label_visibility='collapsed', format_func=lambda i:f"{effective_due(mapping[i]).astimezone(KST):%m/%d %H:%M} · {mapping[i]['title']} · {mapping[i]['minutes']}분")
        task = mapping[ident]
        st.caption(f"{task['category']} · {PRIORITIES[task['priority']]} · {next(k for k,v in REPEATS.items() if v==task['repeat'])}")
        completed = st.checkbox('이 일을 완료했어요', value=False, key=f"complete_{ident}_{task['due']}")
        if completed:
            if not finish_task(task, now):
                archive.append(task)
                st.session_state['tasks'] = [t for t in tasks if t['id'] != ident]
            st.balloons()
            st.rerun()
        c1,c2 = st.columns(2)
        if c1.button('⏳ 10분 뒤 다시', use_container_width=True):
            task.update(snooze=(now+timedelta(minutes=10)).isoformat(), notified=[])
            st.rerun()
        with c2.popover('건너뛰기 / 삭제', use_container_width=True):
            st.caption('건너뛰기는 완료로 집계하지 않습니다. 약 복용 여부는 직접 확인하세요.')
            if st.button('이번 회차 건너뛰기', use_container_width=True):
                if not finish_task(task, now, '건너뜀'):
                    archive.append(task)
                    st.session_state['tasks'] = [t for t in tasks if t['id']!=ident]
                st.rerun()
            consent = st.checkbox('이 일정과 해당 기록을 삭제합니다')
            if st.button('일정 삭제', disabled=not consent, use_container_width=True):
                st.session_state['tasks'] = [t for t in tasks if t['id']!=ident]
                st.rerun()
    else:
        st.caption('할 일을 추가하면 시간순 목록과 추천이 나타납니다.')
    with st.expander('📊 최근 7일 실행 기록'):
        records = [(t,h) for t in all_rows for h in t['history'] if datetime.fromisoformat(h['at'])>=now-timedelta(days=7)]
        if records:
            with st.container(height=180):
                for t,h in sorted(records,key=lambda row:row[1]['at'],reverse=True):
                    stamp = datetime.fromisoformat(h['at']).astimezone(KST)
                    st.text(f"{stamp:%m/%d %H:%M} · {h['status']} · {t['title']}")
        else:
            st.caption('완료·건너뛰기 기록이 여기에 쌓입니다.')
        st.caption('반복 일정 완료 시 다음 회차가 자동 생성됩니다. 놓친 회차를 완료 처리하지는 않습니다. 목록은 선택형으로 압축했지만 작은 화면·키보드·펼친 영역에서는 스크롤이 생길 수 있습니다.')


if __name__ == '__main__':
    main()

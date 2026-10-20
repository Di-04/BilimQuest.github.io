#!/usr/bin/env python3
"""BILIM QUEST: zero-framework classroom game server (Python 3.10+)."""
import json, os, re, secrets, threading, time, io, csv, subprocess, tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import Counter

ROOT=Path(__file__).resolve().parent
PUBLIC=ROOT/'public'; DB=ROOT/'data'/'rooms.json'
LOCK=threading.RLock()
ROOMS={}
if DB.exists():
    try: ROOMS=json.loads(DB.read_text('utf-8'))
    except Exception: ROOMS={}

def persist():
    DB.parent.mkdir(exist_ok=True)
    tmp=DB.with_suffix('.tmp'); tmp.write_text(json.dumps(ROOMS,ensure_ascii=False,indent=2),'utf-8'); tmp.replace(DB)

def public_room(room, teacher=False):
    players=list(room['players'].values())
    scores=[]
    for p in players:
        scored=sum(float(v['points']) for v in p.get('answers',{}).values() if v.get('status')=='graded')
        max_available=sum(float(q['points']) for q in room['questions']) or 1
        academic=round(scored/max_available*10,1)
        scores.append({'id':p['id'],'name':p['name'],'avatar':p['avatar'],'score':academic,'xp':p.get('xp',0),'answered':len(p.get('answers',{})),'finished':p.get('finished',False)})
    scores.sort(key=lambda x:(-x['score'],-x['xp'],x['name']))
    d={'code':room['code'],'name':room['name'],'subject':room.get('subject',''),'status':room['status'], 'language':room['language'],'questionsCount':len(room['questions']), 'criteria':room['criteria'],'players':scores,'reflections':room.get('reflections',[]),'created':room['created'],'teacherMessage':room.get('teacherMessage','')}
    if teacher: d['questions']=room['questions']; d['teacherToken']=room['teacherToken']; d['results']=players
    else:
        d['questions']=[{k:v for k,v in q.items() if k not in ('answer','solution')} for q in room['questions']] if room['status']=='running' else []
        d['reflections']=[{'hard':r.get('hard',''),'feeling':r.get('feeling',''),'message':r.get('message','')} for r in room.get('reflections',[])]
    return d

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(PUBLIC),**kw)
    def log_message(self,fmt,*a): print('[HTTP]',fmt%a)
    def sendjson(self,obj,status=200):
        body=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Access-Control-Allow-Origin','*');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def readjson(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>2_000_000: raise ValueError('Payload too large')
        return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        p=urlparse(self.path); segments=p.path.strip('/').split('/')
        if p.path=='/api/health':return self.sendjson({'ok':True,'rooms':len(ROOMS)})
        if len(segments)>=3 and segments[0]=='api' and segments[1]=='room':
            code=segments[2].upper()
            with LOCK:
                r=ROOMS.get(code)
                if not r:return self.sendjson({'error':'Room not found'},404)
                teacher=parse_qs(p.query).get('token',[''])[0]==r['teacherToken']
                return self.sendjson(public_room(r,teacher))
        if p.path=='/':self.path='/index.html'
        return super().do_GET()
    def do_POST(self):
        try:
            p=urlparse(self.path).path; a=self.readjson()
            with LOCK:
                if p=='/api/room':
                    code=''.join(secrets.choice('0123456789') for _ in range(6))
                    while code in ROOMS:code=''.join(secrets.choice('0123456789') for _ in range(6))
                    token=secrets.token_urlsafe(20)
                    raw=a.get('questions',[])
                    if not raw or len(raw)>80:raise ValueError('Add 1–80 questions')
                    qs=[]
                    for i,q in enumerate(raw):
                        typ=q.get('type','choice')
                        if typ not in ('choice','text'): raise ValueError('Invalid question type')
                        pts=max(.5,min(10,float(q.get('points',1))))
                        options=[str(v)[:220] for v in q.get('options',[])][:6]
                        if typ=='choice' and (len(options)<2 or not str(q.get('answer','')).strip()):raise ValueError('Question needs options and correct answer')
                        qs.append({'id':str(i+1),'level':max(1,min(5,int(q.get('level',1)))),'type':typ,'text':str(q.get('text',''))[:2000],'options':options,'answer':str(q.get('answer',''))[:2000],'criterion':str(q.get('criterion','Knowledge'))[:120], 'points':pts,'time':max(10,min(300,int(q.get('time',45))))})
                    criteria=a.get('criteria',[])
                    r={'code':code,'teacherToken':token,'name':str(a.get('name','New quest'))[:100],'subject':str(a.get('subject',''))[:100],'language':a.get('language','ru'),'questions':qs,'criteria':criteria,'players':{},'reflections':[],'status':'lobby','teacherMessage':'','created':int(time.time())}
                    ROOMS[code]=r;persist();return self.sendjson({'code':code,'token':token})
                m=re.fullmatch(r'/api/room/(\d{6})/(join|start|answer|finish|reflect|grade|close)',p)
                if not m:return self.sendjson({'error':'Endpoint not found'},404)
                code,action=m.groups();r=ROOMS.get(code)
                if not r:return self.sendjson({'error':'Room not found'},404)
                if action=='join':
                    if r['status']!='lobby':raise ValueError('Game already started')
                    if len(r['players'])>=100:raise ValueError('Room is full')
                    name=str(a.get('name','')).strip()[:30]
                    if not name:raise ValueError('Enter your name')
                    pid=secrets.token_urlsafe(12)
                    r['players'][pid]={'id':pid,'name':name,'avatar':str(a.get('avatar','🦊'))[:8],'answers':{},'xp':0,'finished':False}
                    persist();return self.sendjson({'playerId':pid})
                if action in ('start','close','grade'):
                    if a.get('token')!=r['teacherToken']:return self.sendjson({'error':'Teacher access required'},403)
                    if action=='start':r['status']='running'
                    elif action=='close':r['status']='ended'
                    else:
                        player=r['players'].get(a.get('playerId'));qid=str(a.get('questionId',''))
                        q=next((x for x in r['questions'] if x['id']==qid),None)
                        if not player or not q or qid not in player['answers']:raise ValueError('Answer not found')
                        score=max(0,min(float(q['points']),float(a.get('points',0))))
                        player['answers'][qid].update({'points':score,'status':'graded'})
                    persist();return self.sendjson({'ok':True})
                pid=a.get('playerId');player=r['players'].get(pid)
                if not player:return self.sendjson({'error':'Player not found'},403)
                if action=='answer':
                    if r['status']!='running':raise ValueError('Game not running')
                    qid=str(a.get('questionId',''));q=next((x for x in r['questions'] if x['id']==qid),None)
                    if not q:raise ValueError('Question not found')
                    if qid in player['answers']:raise ValueError('Already answered')
                    answer=str(a.get('answer',''))[:2000]
                    correct=q['type']=='choice' and answer==q['answer']
                    points=float(q['points']) if correct else 0
                    player['answers'][qid]={'answer':answer,'correct':correct if q['type']=='choice' else None,'points':points,'status':'graded' if q['type']=='choice' else 'pending'}
                    if correct:player['xp']+=100+int(q['level'])*30
                    persist();return self.sendjson({'correct':correct if q['type']=='choice' else None,'pending':q['type']=='text','points':points})
                if action=='finish':player['finished']=True;persist();return self.sendjson({'ok':True})
                if action=='reflect':
                    if any(x.get('playerId')==pid for x in r['reflections']):raise ValueError('Reflection already sent')
                    r['reflections'].append({'playerId':pid,'hard':str(a.get('hard',''))[:200],'feeling':str(a.get('feeling',''))[:60],'message':str(a.get('message',''))[:500]});persist();return self.sendjson({'ok':True})
        except (ValueError,KeyError,TypeError) as e:return self.sendjson({'error':str(e)},400)
        except Exception as e:print('ERROR',repr(e));return self.sendjson({'error':'Server error'},500)

if __name__=='__main__':
    port=int(os.environ.get('PORT','8000'))
    print(f'BILIM QUEST http://localhost:{port} — share your computer LAN IP for devices on same WiFi')
    ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()

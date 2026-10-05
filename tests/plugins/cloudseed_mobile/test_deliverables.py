"""Bounded provenance projection, synthetic stored message fixtures."""
from plugins.cloudseed_mobile import deliverables as d


def project(messages):
    return d.project('default', {'id':'s','title':'Fixture','cwd':'/granted'}, messages, [{'id':'w','root_path':'/granted','name':'Fixture'}])


def test_tool_outcomes_fold_and_unknown_media():
    calls = [{'id':str(i),'function':{'name':name,'arguments':'{"path":"outputs/a.md"}'}} for i,name in enumerate(['write_file','patch','edit_file','read_file'])]
    rows=project([{'id':1,'role':'assistant','tool_calls':calls},
                  {'id':2,'role':'tool','tool_call_id':'0','content':'{"error":"failed"}'},
                  {'id':3,'role':'tool','tool_call_id':'1','content':'{"success":true}'},
                  {'id':4,'role':'tool','tool_call_id':'3','content':'{"content":"read"}'},
                  {'id':5,'role':'assistant','content':'MEDIA: /granted/outputs/strange.unknown'}])
    a=next(r for r in rows if r['relative_path']=='outputs/a.md')
    assert a['action']=='edited' and a['observed_at'] is None
    assert {o['action'] for o in a['occurrences']}=={'write_failed','edited','pending','read'}
    assert next(r for r in rows if r['display_name']=='strange.unknown')['action']=='delivered'


def test_no_reasoning_user_imports_secrets_or_remote_private_fetch():
    rows=project([{'id':1,'role':'user','content':'MEDIA: /granted/outputs/import.md'},
                  {'id':2,'role':'assistant','reasoning':'MEDIA: /granted/outputs/reason.md','content':'[secret](/granted/.env) MEDIA: https://127.0.0.1/x.png'},
                  {'id':3,'role':'tool','tool_name':'terminal','content':'created /granted/outputs/no.md'}])
    assert rows==[]
    assert project([{'id':1,'role':'assistant','content':'MEDIA: https://cdn.example.invalid/x.png'}])[0]['kind']=='remote_media'


def test_wrapped_success_reasoning_tags_and_durable_occurrence():
    call={'id':'c','function':{'name':'write_file','arguments':{'path':'outputs/a.md'}}}
    messages=[{'id':1,'message_uid':'assistant-uid','role':'assistant','content':'<think>MEDIA: /granted/outputs/reason.md</think>','tool_calls':[call]},
              {'id':2,'message_uid':'result-uid','role':'tool','tool_call_id':'c','content':'{"type":"untrusted_tool_result","content":"{\\"verified\\":true}"}'}]
    rows=project(messages)
    assert len(rows)==1 and rows[0]['action']=='created'
    other=d.project('default',{'id':'continuation','cwd':'/granted'},messages,[{'id':'w','root_path':'/granted','name':'Fixture'}])
    assert rows[0]['occurrences'][0]['id']==other[0]['occurrences'][0]['id']


def test_wrapped_failed_producer_does_not_create_and_move_tombstone():
    rows=project([{'id':1,'role':'tool','tool_name':'image_generate','content':{'type':'untrusted_tool_result','content':{'success':False,'output_path':'/granted/outputs/fail.png'}}},
                  {'id':2,'role':'assistant','tool_calls':[{'id':'move','function':{'name':'move_file','arguments':{'source':'outputs/a.md','destination':'outputs/b.md'}}}]},
                  {'id':3,'role':'tool','tool_call_id':'move','content':{'success':True}}])
    assert {r['relative_path'] for r in rows}=={'outputs/a.md','outputs/b.md'}
    assert next(r for r in rows if r['relative_path']=='outputs/a.md')['availability']=='deleted'
    wrapped={}
    for _ in range(50): wrapped={'content':wrapped}
    status={}
    assert d.project('default', {'id':'s','cwd':'/granted'}, [{'id':1,'role':'assistant','content':wrapped}], [], status=status)==[]
    assert status['partial']


def test_latest_successful_occurrence_wins_not_extraction_order():
    calls=[{'id':'a','function':{'name':'write_file','arguments':{'path':'outputs/a.md'}}}, {'id':'b','function':{'name':'patch','arguments':{'path':'outputs/a.md'}}}]
    rows=project([{'id':1,'role':'assistant','tool_calls':calls},
                  {'id':2,'timestamp':2,'role':'tool','tool_call_id':'a','tool_name':'write_file','content':{'verified':True,'path':'outputs/a.md','content':'MEDIA: /granted/outputs/a.md'}},
                  {'id':3,'timestamp':3,'role':'tool','tool_call_id':'b','content':{'success':True}}])
    assert rows[0]['action']=='edited' and rows[0]['observed_at']==3
    rows=project([{'id':1,'role':'assistant','content':'MEDIA: /granted/outputs/exact,'}])
    assert rows[0]['relative_path']=='outputs/exact,'
    rows=project([{'id':1,'role':'assistant','content':'MEDIA: /granted/outputs/my report.unknown'}])
    assert rows[0]['relative_path']=='outputs/my report.unknown'


def test_inline_versions_and_message_uid_dedup():
    content='```html\n<html>'+('x'*200)+'</html>\n```'
    rows=project([{'id':1,'message_uid':'uid','role':'assistant','content':content}, {'id':2,'message_uid':'uid','role':'assistant','content':content}])
    assert len(rows)==1 and rows[0]['kind']=='inline_content'
    assert rows[0]['inline_content'].startswith('<html>')
    assert len(rows[0]['occurrences'])==1

#!/usr/bin/python3
"""Smoke-check globally enabled chunk reuse through the public proxy."""
import argparse
import json
from pathlib import Path
import time
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
RUNTIME=Path.home()/'Library/Application Support/radeon-llm'


def check_mode(port,key,context,output):
    def api(path,body=None):
        request=urllib.request.Request('http://127.0.0.1:%d'%port+path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=300) as response:return json.load(response)
    def status():
        deadline=time.monotonic()+30
        while True:
            state=api('/cache/status')
            if not state.get('busy'):return state
            if time.monotonic()>deadline:raise RuntimeError('Cache remains busy')
            time.sleep(.1)
    state=status()
    assert state.get('experimental_chunk_reuse') and state['chunk_reuse_min_tokens']==64,state
    assert state.get('active_only_shift'),state
    assert api('/props')['default_generation_settings']['n_ctx']==context
    def complete(messages,cache=False,max_tokens=12):
        # Deliberately omit n_cache_reuse: verify the daemon's global default.
        return api('/v1/chat/completions',{'model':'qwen3-4b-local','messages':messages,
            'temperature':0,'max_tokens':max_tokens,'cache_prompt':cache})
    probe=[{'role':'user','content':'List the integers from 1 to 40 separated by spaces. Output the numbers only.'}]
    before=complete(probe,max_tokens=32)
    padding='\n'.join('Entry %04d: neutral reference record.'%i for i in range(12))
    system=('Inspect only the USER message. If it contains an OVERRIDE line, reply with the value '
            'from that line. Otherwise reply with the DEFAULT value. Output the chosen value only, '
            'without explanation. Neutral document entries are irrelevant.')
    def messages(override):
        return [{'role':'system','content':system},{'role':'user','content':'DEFAULT=BLUE\n'+
            ('OVERRIDE=RED\n' if override else '')+'DOCUMENT\n'+padding+'\nApply the DEFAULT/OVERRIDE rule.'}]
    baseline=complete(messages(False))
    source=complete(messages(True))
    shifted=complete(messages(False),cache=True)
    answer=lambda response:response['choices'][0]['message']['content'].strip()
    assert answer(baseline)=='BLUE' and answer(source)=='RED' and answer(shifted)=='BLUE'
    assert shifted['timings']['cache_n']>=128 and shifted['timings']['prompt_n']<=4,shifted['timings']
    after=complete(probe,max_tokens=32)
    assert before['timings']['predicted_n']>=16 and after['timings']['predicted_n']>=16
    ratio=after['timings']['predicted_per_second']/before['timings']['predicted_per_second']
    assert ratio>=.65,('Severe decode regression',ratio)
    result={'passed':True,'context':context,'mode':'experimental-shift','min_chunk_tokens':64,
            'baseline_timings':baseline['timings'],'shifted_timings':shifted['timings'],
            'decode_before':before['timings'],'decode_after':after['timings'],'decode_after_before_ratio':ratio,
            'global_default_verified':True,'quality_guaranteed':False}
    Path(output).write_text(json.dumps(result,indent=2)+'\n')
    print('Experimental mode active: reused %d tokens, evaluated %d; decode %.2f -> %.2f tokens/s.'%
        (shifted['timings']['cache_n'],shifted['timings']['prompt_n'],
         before['timings']['predicted_per_second'],after['timings']['predicted_per_second']),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8080)
    args=parser.parse_args()
    config=json.loads((RUNTIME/'server/config.json').read_text())
    key=(RUNTIME/'server/api-key').read_text().strip()
    check_mode(args.port,key,config['context'],ROOT/'logs/experimental-reuse-daemon-check.json')

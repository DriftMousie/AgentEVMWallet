"""Exit precondition evidence; never simulates a mutating entry or broadcasts."""
import re
from .abi import canonical,encode_call,resolve
from .common import ToolError,formatted,require,uint,units
from .patternmatch import expression, inspect_patterns


def single(row):
    if row.get('status')!='success' or len(row.get('values',[]))!=1:return None
    return row['values'][0]


def numeric(value):return isinstance(value,(str,int)) and not isinstance(value,bool) and str(value).isdigit()


def unsigned(row):
    types=row.get('types',[])
    return single(row) if len(types)==1 and re.fullmatch(r'uint(?:[0-9]+)?',types[0]) else None


def diagnose_contract(inspector,root,sem):
    p=inspector.params;unit=p.get('amount_unit','shares');require(unit in ('shares','assets'),'amount_unit must be shares or assets')
    require(p.get('amount') is None or p.get('amount_raw') is None,'Use amount or amount_raw, not both')
    bindings={'root':root,'account':inspector.account,'amount_raw':None}
    found, patterns_status, relations = inspect_patterns(inspector, root, p.get('pattern', 'auto'))
    bindings.update(relations)
    patterns=[{k:v for k,v in f.items() if k!='definition'} for f in found]
    verdict={'question':'can_redeem','answer':'unknown','confidence':'low','scope':'supported_read_only_preconditions_not_execution_or_audit','entry':None,'checks':[],
             'estimated_out':None,'blockers':[],'unknowns':[]}
    result={'patterns':patterns,'patterns_status':patterns_status,'verdict':verdict}
    def add(id,status='unknown',severity='blocker',**extra):
        row={'id':id,'status':status,'severity':severity,**extra};verdict['checks'].append(row)
        if status=='fail' and severity=='blocker':verdict['blockers'].append(row)
        elif status!='pass':verdict['unknowns'].append(row)
        return row
    if len(found)!=1:
        reason = 'pattern_evidence_incomplete' if patterns_status != 'complete' else 'no_matching_verified_interface'
        add('pattern',reason=reason if not found else 'multiple_patterns_need_explicit_selection');return result
    if patterns_status != 'complete':
        add('pattern', reason='pattern_evidence_incomplete')
    definition=found[0]['definition']
    def dispatch_known(target):
        structure=inspector.structure(target)
        return structure['resolution_status']=='complete' and all(layer.get('verified') is True or layer.get('pointer_evidence') in ('exact_runtime_pattern','delegation_marker') for layer in structure['layers'])
    if not dispatch_known(root):add('proxy_dispatch',reason='unverified_or_unconfirmed_proxy_dispatch')
    asset_binding = definition['asset_binding']
    if asset_binding not in bindings:
        bindings[asset_binding] = inspector.relation(root, asset_binding)
    asset=bindings.get(asset_binding)
    decimals_target=root if unit=='shares' else asset
    decimals=unsigned(inspector.read(decimals_target,'decimals()')) if decimals_target else None
    if p.get('amount_raw') is not None:bindings['amount_raw']=str(uint(p['amount_raw'],'amount_raw'))
    elif p.get('amount') is not None:
        if numeric(decimals) and int(decimals)<=255:bindings['amount_raw']=str(units(p['amount'],int(decimals)))
        else:add('amount_precision',reason='decimals_unavailable')
    if bindings['amount_raw'] is not None:require(int(bindings['amount_raw'])>0,'Diagnostic amount must be positive')
    if not bindings['account']:add('account',reason='pending_account')
    if bindings['amount_raw'] is None:add('amount',reason='pending_amount')
    if not sem:add('source_coverage',reason='verified_source_not_available_or_disabled')
    elif sem.get('semantics_partial'):add('source_coverage',reason='source_extraction_partial')
    route_ok=True
    guard=definition.get('route_guard')
    if guard:
        modifiers=[m for m in (sem or {}).get('modifiers',[]) if m['name']==guard['modifier'] and any(re.sub(r'\s+','',c['expr'])==guard['predicate'] for c in m['conditions'])]
        uses=[f for f in (sem or {}).get('functions',[]) if f.get('signature')==guard['functions'][unit] and guard['modifier'] in f.get('modifiers',[])]
        route_ok=len(modifiers)==1 and len(uses)==1
        add('core_route','pass' if route_ok else 'unknown',evidence={'modifier':modifiers[0]['evidence'],'function':uses[0]['evidence']} if route_ok else None,reason=None if route_ok else 'route_source_predicate_not_established')
    for descriptor in definition.get('reads',[])+definition.get('reads_by_unit',{}).get(unit,[]):
        target=bindings.get(descriptor['target']);value=None
        if target:
            if 'relation' in descriptor:
                value=inspector.relation(target,descriptor['relation'])
                if value and inspector.abi(value) is None:add('dependency_'+descriptor['bind'],reason='effective_dependency_abi_unverified_or_unavailable',address=value)
            else:
                try:
                    args=[expression(v,bindings) for v in descriptor['args']]
                    row=inspector.read(target,descriptor['function'],args);value=unsigned(row)
                    if not numeric(value):value=None
                except ToolError:pass
        bindings[descriptor['bind']]=value
    blocked=set()
    for descriptor in definition.get('source_guards',[]):
        defs=[f for f in (sem or {}).get('functions',[]) if f['name']==descriptor['function']]
        needle=re.sub(r'\s+','',descriptor['condition_contains'])
        evidence=[c['evidence'] for f in defs for c in f.get('conditions',[]) if needle in re.sub(r'\s+','',c['expr'])]
        if len(defs)!=1 or len(evidence)!=1:
            blocked.update(descriptor['gates']);add(descriptor['id'],reason='source_formula_not_established')
    for name,expr in definition['equations'][unit].items():
        if name in blocked:continue
        try:bindings[name]=expression(expr,bindings)
        except ToolError:bindings[name]=None
    for descriptor in definition.get('unknowns',[]):add(descriptor['id'],severity=descriptor['severity'],reason=descriptor['reason'],address=bindings.get(descriptor['target']))
    for check in definition['checks']:
        if check.get('unit') and check['unit']!=unit:continue
        args=[]
        try:
            if any(bindings.get(n) is None for n in check.get('needs',[])):raise ToolError('VALUE_UNKNOWN','pending_account_or_amount')
            target=bindings.get(check['target']);require(target is not None,'Dependency unavailable','DEPENDENCY_UNKNOWN')
            args=[expression(v,bindings) for v in check['args']];probe=inspector.read(target,check['function'],args)
            require(probe.get('status')=='success',probe.get('reason','Probe unsuccessful'),'CHECK_UNKNOWN')
            observed=probe.get('named_values',{}).get(check['field']) if check.get('field') else single(probe)
            require(observed is not None,'Output field unavailable','CHECK_UNKNOWN')
            passed=True;required=None
            if 'expect' in check:
                required=expression(check['expect'],bindings)
                require(isinstance(observed,bool)==isinstance(required,bool),'Output type differs from expected check','CHECK_UNKNOWN')
                if numeric(required):
                    require(unsigned(probe) is not None,'Unsigned output type required','CHECK_UNKNOWN');passed=int(observed)==int(required)
                else:passed=type(observed)==type(required) and observed==required
            if 'gte' in check:
                required=expression(check['gte'],bindings);require(unsigned(probe) is not None and numeric(observed) and numeric(required),'Unsigned numeric output required','CHECK_UNKNOWN');passed=int(observed)>=int(required)
            if check.get('output_type')=='uint':require(unsigned(probe) is not None,'Unsigned output type is unknown','CHECK_UNKNOWN')
            add(check['id'],'pass' if passed else 'fail',check['severity'],observed=observed,required=required,evidence={'address':target,'function':check['function'],'args':args,'raw':probe.get('raw'),'block':inspector.block})
            bindings[check['id']]=observed
        except ToolError as exc:add(check['id'],severity=check['severity'],reason=exc.code)
    spec=definition['entries'][unit]
    if bindings['account'] and bindings['amount_raw'] is not None and route_ok:
        try:
            target=bindings.get(spec['target']);abi=inspector.abi(target) if target else None;require(abi,'Entry ABI unavailable','ABI_REQUIRED')
            require(dispatch_known(target),'Entry dispatch code is unverified or unresolved','ROUTE_UNKNOWN')
            item=resolve(abi,spec['function']);args=[expression(v,bindings) for v in spec['args']]
            verdict['entry']={'contract':target,'function':canonical(item),'args':args,'data':encode_call(canonical(item),args),'value_wei':'0','status':'candidate_not_simulated','caller':bindings['account']}
            if definition.get('entry_source_required',True):
                defs=[f for f in (sem or {}).get('functions',[]) if f.get('signature')==canonical(item)]
                if len(defs)!=1:add('entry_source_coverage',reason='entry_definition_missing_or_ambiguous')
                elif defs[0].get('modifiers') or defs[0].get('conditions') or defs[0].get('calls'):
                    add('entry_source_conditions',reason='entry_has_unverified_modifiers_conditions_or_external_calls',evidence=defs[0]['evidence'])
        except ToolError as exc:add('entry',reason=exc.code)
    assets=bindings.get(definition['estimate']['binding'])
    if numeric(assets):
        asset_dec=unsigned(inspector.read(asset,'decimals()')) if asset else None
        symbol=single(inspector.read(asset,'symbol()')) if asset else None
        verdict['estimated_out']={'amount_raw':str(assets),'amount':formatted(assets,int(asset_dec)) if numeric(asset_dec) and int(asset_dec)<=255 else None,'symbol':symbol if isinstance(symbol,str) else '?','address':asset,'scope':definition['estimate']['scope'],'fee':'unknown','net_amount_raw':str(assets) if definition['estimate']['net_known'] else None}
    if verdict['blockers']:verdict.update(answer='likely_no',confidence='medium')
    elif any(r['severity']=='blocker' for r in verdict['unknowns']):verdict.update(answer='unknown',confidence='low')
    else:verdict.update(answer='likely_yes',confidence='medium')
    verdict['unknowns'].append({'id':'execution_not_simulated','severity':'info','status':'unknown','reason':'Only verified ABI view/pure calls were made; no actual exit or audit was performed'})
    return result

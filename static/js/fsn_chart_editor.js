/* DairyMitr FAT/SNF chart editor - shared 0.1-step rate increment. */
(function () {
  "use strict";
  var cfg = FSN.readJSON("chartData") || {};
  var chart = cfg.chart || null;
  var source = cfg.source || null;
  var reviseId = cfg.revise_id || null;
  var $ = function (id) { return document.getElementById(id); };
  var state = { fats: [], snfs: [], cells: {}, original: {}, dirty: false };
  var FIXED = { fat_min: "3.0", fat_max: "5.0", fat_step: "0.1", snf_min: "8.1", snf_max: "9.0", snf_step: "0.1", std_fat: "3.5", std_snf: "8.5" };

  function k(v) { return Number(v).toFixed(2); }
  function cellKey(f, s) { return k(f) + "|" + k(s); }
  function setDirty(v) { state.dirty = v; }
  function setFixedFields() { Object.keys(FIXED).forEach(function (id) { if ($(id)) $(id).value = FIXED[id]; }); syncSharedStep(); }
  function syncSharedStep() { if ($("snf_rate_step") && $("fat_rate_step")) $("snf_rate_step").value = $("fat_rate_step").value; }
  function moneyRate(f, s) {
    var base = Number($("std_rate").value);
    var step = Number($("fat_rate_step").value);
    return Math.round((base + ((Number(f) - 3.5) / 0.1) * step + ((Number(s) - 8.5) / 0.1) * step + Number.EPSILON) * 100) / 100;
  }
  function ruleExplain() {
    var step = Number($("fat_rate_step").value || 0);
    $("ruleExplain").textContent = "आधार: FAT 3.5 / SNF 8.5 = ₹" + (Number($("std_rate").value || 0)).toFixed(2) + ". FAT किंवा SNF प्रत्येक 0.1 ने बदलल्यास दर ₹" + Math.abs(step).toFixed(2) + " ने " + (step >= 0 ? "वाढेल" : "कमी होईल") + ".";
  }
  function clearErrors() {
    document.querySelectorAll(".fsn-field").forEach(function (f) { f.classList.remove("invalid"); var e=f.querySelector(".fsn-err"); if(e)e.textContent=""; });
  }
  function fieldError(field, msg) {
    var f=document.querySelector('.fsn-field[data-field="'+field+'"]');
    if (!f) return false;
    f.classList.add("invalid"); var e=f.querySelector(".fsn-err"); if(e)e.textContent=msg;
    var input=f.querySelector("input,select"); if(input)input.focus(); return true;
  }
  function settingsPayload() {
    setFixedFields(); syncSharedStep();
    var out={};
    ["fat_min","fat_max","fat_step","snf_min","snf_max","snf_step","std_fat","std_snf","std_rate","fat_rate_step","snf_rate_step"].forEach(function(id){out[id]=$(id).value.trim();});
    return out;
  }
  function render() {
    var table=$("matrix"); table.innerHTML="";
    var thead=document.createElement("thead"), hr=document.createElement("tr");
    hr.appendChild(FSN.el("th",{"class":"corner","scope":"col"},"FAT \\ SNF"));
    state.snfs.forEach(function(s){
      var th=FSN.el("th",{scope:"col"},s), rm=FSN.el("button",{type:"button","class":"axis-remove",title:"SNF स्तंभ काढा","aria-label":"SNF "+s+" काढा"},"×");
      rm.addEventListener("click",function(){removeAxis("snf",s);}); th.appendChild(rm); hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    var tbody=document.createElement("tbody");
    state.fats.forEach(function(f,r){
      var tr=document.createElement("tr"), th=FSN.el("th",{scope:"row"},f), rm=FSN.el("button",{type:"button","class":"axis-remove",title:"FAT ओळ काढा","aria-label":"FAT "+f+" काढा"},"×");
      rm.addEventListener("click",function(){removeAxis("fat",f);}); th.appendChild(rm); tr.appendChild(th);
      state.snfs.forEach(function(s,c){
        var key=cellKey(f,s), td=document.createElement("td"), input=FSN.el("input",{type:"number",min:"0",step:"0.01",inputmode:"decimal","data-r":r,"data-c":c,"aria-label":"FAT "+f+" SNF "+s+" दर"});
        if (key===cellKey(3.5,8.5)) td.classList.add("standard-rate");
        input.value=state.cells[key]===undefined?"":state.cells[key]; markCell(td,key,input.value);
        input.addEventListener("input",function(){state.cells[key]=input.value.trim();setDirty(true);markCell(td,key,input.value.trim());});
        input.addEventListener("keydown",navigate); td.appendChild(input); tr.appendChild(td);
      }); tbody.appendChild(tr);
    });
    table.appendChild(tbody); $("matrixSection").hidden=false; $("saveBtn").disabled=false;
    $("matrixInfo").textContent="("+state.fats.length+" FAT × "+state.snfs.length+" SNF = "+(state.fats.length*state.snfs.length)+" दर)";
  }
  function markCell(td,key,value){
    var bad=value===""||!Number.isFinite(Number(value))||Number(value)<0;
    td.classList.toggle("bad",bad);
    td.classList.toggle("changed",!bad&&state.original[key]!==undefined&&Number(state.original[key])!==Number(value));
    td.classList.toggle("changed-rate",!bad&&state.original[key]!==undefined&&Number(state.original[key])!==Number(value));
  }
  function navigate(e){
    var r=Number(this.dataset.r),c=Number(this.dataset.c),moves={ArrowUp:[-1,0],ArrowDown:[1,0],Enter:[1,0],ArrowLeft:[0,-1],ArrowRight:[0,1]},m=moves[e.key];
    if(!m)return; if((e.key==="ArrowLeft"||e.key==="ArrowRight")&&this.selectionStart!==this.selectionEnd)return;
    var next=document.querySelector('#matrix input[data-r="'+(r+m[0])+'"][data-c="'+(c+m[1])+'"]'); if(next){e.preventDefault();next.focus();next.select();}
  }
  function removeAxis(kind,value){var list=kind==="fat"?state.fats:state.snfs;if(list.length<=1)return;if(!confirm((kind==="fat"?"FAT ":"SNF ")+value+" काढायचे?"))return;list.splice(list.indexOf(value),1);setDirty(true);render();}
  function addAxis(kind){
    var list=kind==="fat"?state.fats:state.snfs, input=$(kind==="fat"?"addFat":"addSnf");
    var v=input.value.trim()?Number(input.value):((list.length?Math.max.apply(null,list.map(Number)):0)+0.1);
    v=Math.round(v*100)/100;
    if(!Number.isFinite(v)||v<=0||v>20||Math.abs(v*10-Math.round(v*10))>0.00001){FSN.notice("editorAlert","कृपया 0.1 च्या पटीतील योग्य मूल्य भरा (उदा. "+(kind==="fat"?"5.1":"9.1")+").","error");input.focus();return;}
    var key=k(v); if(list.indexOf(key)!==-1){FSN.notice("editorAlert",key+" आधीच आहे.","error");return;}
    list.push(key);list.sort(function(a,b){return Number(a)-Number(b);});
    // Fill only missing cells; manually edited and existing cells are never overwritten.
    state.fats.forEach(function(f){state.snfs.forEach(function(s){var ck=cellKey(f,s);if(state.cells[ck]===undefined||state.cells[ck]==="")state.cells[ck]=moneyRate(f,s).toFixed(2);});});
    input.value="";setDirty(true);FSN.notice("editorAlert","नवीन "+kind.toUpperCase()+" "+key+" जोडला. नवीन दर आपोआप भरले आहेत; गरज असल्यास बदलू शकता.","ok");render();
  }
  function generate(){
    clearErrors(); FSN.notice("editorAlert",""); setFixedFields();
    var base=Number($("std_rate").value),step=Number($("fat_rate_step").value);
    if(!Number.isFinite(base)||base<0){fieldError("std_rate","कृपया शून्य किंवा त्यापेक्षा मोठा आधार दर भरा.");return;}
    if(!Number.isFinite(step)){fieldError("fat_rate_step","कृपया योग्य दर बदल भरा.");return;}
    if(Object.keys(state.cells).length&&!confirm("नवीन तक्ता तयार केल्यास सध्याचे आणि हाताने बदललेले दर बदलतील. पुढे जायचे?"))return;
    var btn=$("generateBtn");btn.disabled=true;
    FSN.api("/api/fat_snf/charts/preview",{method:"POST",body:settingsPayload()}).then(function(res){
      btn.disabled=false;if(!res.ok){FSN.notice("editorAlert",res.data.message||"तक्ता तयार करता आला नाही.","error");return;}
      state.fats=res.data.fat_values.map(k);state.snfs=res.data.snf_values.map(k);state.cells={};
      res.data.matrix.forEach(function(m){state.cells[cellKey(m.fat,m.snf)]=Number(m.rate).toFixed(2);});
      setDirty(true);render();var section=$("matrixSection");if(section.scrollIntoView)section.scrollIntoView({behavior:"smooth",block:"start"});
    });
  }
  function save(){
    clearErrors();FSN.notice("saveAlert","");var name=$("chartName").value.trim();
    if(!name){fieldError("chart_name","चार्टचे नाव आवश्यक आहे.");return;}
    if(!$("effectiveFrom").value){fieldError("effective_from","लागू तारीख निवडा.");return;}
    var matrix=[],missing=0;
    state.fats.forEach(function(f){state.snfs.forEach(function(s){var v=state.cells[cellKey(f,s)];if(v===undefined||v===""||!Number.isFinite(Number(v))||Number(v)<0)missing++;matrix.push({fat:f,snf:s,rate:v===undefined?"":v});});});
    if(!matrix.length){FSN.notice("saveAlert","आधी तक्ता तयार करा.","error");return;}
    if(missing){FSN.notice("saveAlert",missing+" दर रिकामे किंवा चुकीचे आहेत. लाल रंगातील दर दुरुस्त करा.","error");var bad=document.querySelector("#matrix td.bad input");if(bad)bad.focus();return;}
    var payload={chart_name:name,milk_type:$("milkType").value,effective_from:$("effectiveFrom").value,settings:settingsPayload(),fat_values:state.fats,snf_values:state.snfs,matrix:matrix};
    if(reviseId)payload.revised_from=reviseId;
    var btn=$("saveBtn");btn.disabled=true;var oldText=btn.textContent;btn.textContent="सेव्ह होत आहे...";
    var url=chart?"/api/fat_snf/charts/"+chart.id:"/api/fat_snf/charts";
    FSN.api(url,{method:chart?"PUT":"POST",body:payload,timeout:30000}).then(function(res){
      btn.disabled=false;btn.textContent=oldText||"💾 चार्ट सेव्ह करा";
      if(res.ok){setDirty(false);FSN.notice("saveAlert",res.data.message||"चार्ट सेव्ह झाला.","ok");setTimeout(function(){window.location.href="/fat_snf/rate-chart";},1200);return;}
      var msg=res.data.message||"चार्ट सेव्ह करताना अडचण आली.";
      if(res.network)msg="नेटवर्क अडचण: चार्ट सेव्ह झाला की नाही हे खात्रीने सांगता येत नाही. चार्ट यादी तपासा.";
      if(!fieldError(res.data.field,msg))FSN.notice("saveAlert",msg,"error");
    });
  }
  function prefill(){
    $("effectiveFrom").value=cfg.today||"";setFixedFields();
    if(source){$("chartName").value=source.chart_name||"";$("milkType").value=source.milk_type||"cow";if(chart&&source.effective_from)$("effectiveFrom").value=source.effective_from;
      if(source.std_rate!==undefined&&source.std_rate!==null)$("std_rate").value=source.std_rate;
      var step=source.fat_rate_step!==undefined&&source.fat_rate_step!==null?source.fat_rate_step:(source.snf_rate_step!==undefined?source.snf_rate_step:"0.30");$("fat_rate_step").value=step;syncSharedStep();
      var fs={},ss={};(source.values||[]).forEach(function(v){fs[k(v.fat)]=1;ss[k(v.snf)]=1;state.cells[cellKey(v.fat,v.snf)]=Number(v.rate).toFixed(2);});
      state.fats=Object.keys(fs).sort(function(a,b){return Number(a)-Number(b);});state.snfs=Object.keys(ss).sort(function(a,b){return Number(a)-Number(b);});
      if(chart)state.original=Object.assign({},state.cells);
      if(state.fats.length&&state.snfs.length)render();
    }
    ruleExplain();
  }
  $("fat_rate_step").addEventListener("input",function(){syncSharedStep();ruleExplain();});
  $("std_rate").addEventListener("input",ruleExplain);
  $("generateBtn").addEventListener("click",generate);
  $("addFatBtn").addEventListener("click",function(){addAxis("fat");});
  $("addSnfBtn").addEventListener("click",function(){addAxis("snf");});
  $("saveBtn").addEventListener("click",save);
  window.addEventListener("beforeunload",function(e){if(state.dirty){e.preventDefault();e.returnValue="";}});
  prefill();
})();

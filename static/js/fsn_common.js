/* DairyMitr FAT/SNF shared helpers (no dependencies). */
(function (global) {
  "use strict";
  var NETWORK_MSG = "नेटवर्क कनेक्शनमध्ये अडचण आहे. कृपया पुन्हा प्रयत्न करा.";
  function api(url, options) {
    options = options || {};
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, options.timeout || 15000);
    var init = { method: options.method || "GET", headers: { "Accept": "application/json" }, credentials: "same-origin", signal: controller.signal };
    if (options.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(options.body); }
    return fetch(url, init).then(function (res) {
      clearTimeout(timeout);
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!data.message && !res.ok) data.message = res.status === 401 ? "कृपया पुन्हा लॉगिन करा." : "डेटा सेव्ह करताना अडचण आली. तुमची नोंद पुन्हा तपासा.";
        return { ok: res.ok, status: res.status, data: data, network: false };
      });
    }).catch(function () { clearTimeout(timeout); return { ok: false, status: 0, data: { message: NETWORK_MSG }, network: true }; });
  }
  function uuid() { if (global.crypto && global.crypto.randomUUID) return global.crypto.randomUUID(); return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,function(c){var r=Math.random()*16|0;return(c==="x"?r:(r&0x3|0x8)).toString(16);}); }
  function money(v) { var n=Number(v||0); return "₹"+n.toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2}); }
  function num(v,digits) { if(v===null||v===undefined||v==="")return "–";return Number(v).toFixed(digits===undefined?2:digits); }
  function esc(v) { var d=document.createElement("div");d.textContent=v===null||v===undefined?"":String(v);return d.innerHTML; }
  function el(tag,attrs,text) { var e=document.createElement(tag);Object.keys(attrs||{}).forEach(function(k){if(k==="class")e.className=attrs[k];else e.setAttribute(k,attrs[k]);});if(text!==undefined&&text!==null)e.textContent=text;return e; }
  function notice(target,message,kind,items) { var box=typeof target==="string"?document.getElementById(target):target;if(!box)return;box.innerHTML="";if(!message)return;var div=el("div",{"class":"fsn-notice "+(kind||""),role:kind==="error"?"alert":"status"},message);if(items&&items.length){var ul=el("ul");items.forEach(function(w){ul.appendChild(el("li",{},w));});div.appendChild(ul);}box.appendChild(div); }
  function readJSON(id) { var node=document.getElementById(id);return node?JSON.parse(node.textContent):null; }
  var SLOT={morning:"सकाळ",evening:"संध्याकाळ"},TYPE={cow:"गाय",buffalo:"म्हैस",both:"गाय व म्हैस"};
  global.FSN={api:api,uuid:uuid,money:money,num:num,esc:esc,el:el,notice:notice,readJSON:readJSON,SLOT:SLOT,TYPE:TYPE};
})(window);

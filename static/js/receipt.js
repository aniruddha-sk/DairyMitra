document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("printBtn");
  if (!btn) return;

  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "प्रिंट चालू आहे...";
    setTimeout(() => {
      window.print();
      btn.disabled = false;
      btn.textContent = "🖨️ सर्व रिसीट्स प्रिंट करा";
    }, 300);
  });
});

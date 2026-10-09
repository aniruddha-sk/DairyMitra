// Sidebar Toggle
const toggleBtn = document.querySelector('.toggle-btn');
const sidebar = document.querySelector('.sidebar');

if(toggleBtn){
  toggleBtn.addEventListener('click', () => {
    sidebar.classList.toggle('active');
  });
}

// Settings Menu Toggle
const settingsBtn = document.getElementById('settingsBtn');
const settingsMenu = document.getElementById('settingsMenu');

if(settingsBtn){
  settingsBtn.addEventListener('click', () => {
    settingsMenu.style.display =
      settingsMenu.style.display === "flex" ? "none" : "flex";
  });
}

window.addEventListener('click', (e) => {
  if (!settingsBtn.contains(e.target) && !settingsMenu.contains(e.target)) {
    settingsMenu.style.display = "none";
  }
});

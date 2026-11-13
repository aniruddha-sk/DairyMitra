/* DairyPro AI assistant chat window */
(function(){

  "use strict";

  const aiButton = document.getElementById("dairyAiButton");
  const aiChat = document.getElementById("dairyAiChat");
  const aiClose = document.getElementById("dairyAiClose");
  const input = document.getElementById("dairyAiInput");
  const sendButton = document.getElementById("dairyAiSend");
  const messages = document.getElementById("dairyAiMessages");
  const typing = document.getElementById("dairyAiTyping");

  if(
    !aiButton ||
    !aiChat ||
    !aiClose ||
    !input ||
    !sendButton ||
    !messages ||
    !typing
  ){
    console.error("DairyPro AI: required elements not found.");
    return;
  }

  /* ==========================================================
     OPEN CHAT
  ========================================================== */

  function openDairyAI(){

    aiChat.classList.add("active");

    setTimeout(function(){
      input.focus();
      scrollMessages();
    },100);
  }


  /* ==========================================================
     CLOSE CHAT
  ========================================================== */

  function closeDairyAI(){

    aiChat.classList.remove("active");
  }


  /* ==========================================================
     SCROLL
  ========================================================== */

  function scrollMessages(){

    messages.scrollTop = messages.scrollHeight;
  }


  /* ==========================================================
     ESCAPE HTML
  ========================================================== */

  function escapeDairyAI(text){

    const div = document.createElement("div");

    div.textContent = String(text ?? "");

    return div.innerHTML;
  }


  /* ==========================================================
     FORMAT AI RESPONSE
  ========================================================== */

  function formatDairyAIResponse(text){

    if(!text){

      return t("Sorry, no answer was received from AI.");
    }

    return escapeDairyAI(text)
      .replace(/\n/g,"<br>");
  }


  /* ==========================================================
     ADD MESSAGE
  ========================================================== */

  function addDairyAIMessage(message,type){

    const div = document.createElement("div");

    div.className =
      "dairy-ai-message " +
      (type === "user"
        ? "user-message"
        : "ai-message");

    const bubble = document.createElement("div");

    bubble.className = "dairy-ai-bubble";

    bubble.innerHTML = message;

    div.appendChild(bubble);

    messages.appendChild(div);

    scrollMessages();
  }


  /* ==========================================================
     TYPING
  ========================================================== */

  function showDairyAITyping(){

    typing.style.display = "block";

    scrollMessages();
  }


  function hideDairyAITyping(){

    typing.style.display = "none";
  }


  /* ==========================================================
     SEND MESSAGE
  ========================================================== */

  async function sendDairyAIMessage(){

    const message = input.value.trim();

    if(!message){
      return;
    }
    if(navigator.onLine === false){
      addDairyAIMessage("⚠️ " + escapeDairyAI(t("This action requires internet.")), "ai");
      return;
    }

    /* USER MESSAGE */
    addDairyAIMessage(
      escapeDairyAI(message),
      "user"
    );

    input.value = "";

    /* DISABLE */
    sendButton.disabled = true;
    input.disabled = true;

    /* TYPING */
    showDairyAITyping();

    try{

      const response = await fetch(
        "/api/chat",
        {
          method:"POST",

          headers:{
            "Content-Type":"application/json",
            "Accept":"application/json"
          },

          credentials:"same-origin",

          body:JSON.stringify({
            message:message
          })
        }
      );


      /* RESPONSE JSON */
      let data = {};

      try{
        data = await response.json();
      }
      catch(jsonError){

        throw new Error(
          t("Unexpected answer from the server.")
        );
      }


      hideDairyAITyping();


      /* HTTP ERROR */
      if(!response.ok){

        addDairyAIMessage(
          "❌ " +
          escapeDairyAI(
            data.error ||
            data.message ||
            t("Something went wrong. Please try again.")
          ),
          "ai"
        );

        return;
      }


      /* SUCCESS */
      if(data.success === false){

        addDairyAIMessage(
          "⚠️ " +
          escapeDairyAI(
            data.error ||
            t("Sorry, no answer was received from AI.")
          ),
          "ai"
        );

        return;
      }


      /*
        Backend may return either:
        data.response
        OR
        data.reply
      */

      const aiResponse =
        data.response ??
        data.reply ??
        data.message ??
        "";


      addDairyAIMessage(
        formatDairyAIResponse(aiResponse),
        "ai"
      );

    }
    catch(error){

      hideDairyAITyping();

      console.error(
        "DairyPro AI Error:",
        error
      );

      addDairyAIMessage(
        "❌ " + t("Network problem. Please try again."),
        "ai"
      );

    }
    finally{

      sendButton.disabled = false;
      input.disabled = false;

      input.focus();
    }
  }


  /* ==========================================================
     EVENTS
  ========================================================== */

  aiButton.addEventListener(
    "click",
    openDairyAI
  );


  aiClose.addEventListener(
    "click",
    closeDairyAI
  );


  sendButton.addEventListener(
    "click",
    sendDairyAIMessage
  );


  input.addEventListener(
    "keydown",
    function(event){

      if(event.key === "Enter"){

        event.preventDefault();

        sendDairyAIMessage();
      }
    }
  );


  document.addEventListener(
    "keydown",
    function(event){

      if(
        event.key === "Escape" &&
        aiChat.classList.contains("active")
      ){

        closeDairyAI();
      }
    }
  );


  /* ==========================================================
     GLOBAL FUNCTIONS
     ==========================================================
     Keeps compatibility with the original inline
     onclick="openDairyAI()" / closeDairyAI() approach.
  ========================================================== */

  window.openDairyAI = openDairyAI;
  window.closeDairyAI = closeDairyAI;
  window.sendDairyAIMessage = sendDairyAIMessage;

})();

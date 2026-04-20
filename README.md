# Accuretta

Accuretta is a fully local AI bridge and Integrated Development Environment designed to give you a private, autonomous assistant right on your machine. It operates entirely offline without telemetry, cloud dependencies, or mandatory accounts. The application connects to your local Ollama instance and provides a robust web interface that you can access from your desktop or over your local network on a mobile device.

### Core Features

* **Three Unique Modes:**
  * **IDE Mode:** Ask the agent to build web pages or interfaces. The application will render the resulting code live in a dedicated preview pane and automatically save versions for you to compare.
  * **Agent Mode:** The model can securely list directories, read files, write files, and execute PowerShell commands. Every action that modifies your system is halted and placed in a queue for your explicit approval.
  * **Auto Mode:** The bridge intelligently selects the appropriate mode based on your request.
* **Complete Privacy:** All conversations, workspace settings, and system context files are stored locally in your directory. Nothing leaves your computer.
* **System Awareness:** On the first launch, the application creates a localized context file. This tells the AI precisely where your desktop, documents, and other crucial folders reside so it can navigate your file system intelligently.
* **Desktop Automation:** Experimental support for vision based screen reading and synthetic mouse and keyboard inputs.

### Target Audiences

* **Privacy Conscious Developers:** Engineers who need an AI assistant that cannot phone home with proprietary source code.
* **Local Automators:** Power users looking to automate repetitive tasks on Windows without writing the scripts themselves.
* **Rapid Prototypers:** Designers and frontend engineers who want to describe a web component and see it rendered immediately.

### Prominent Use Cases

* **Coding and Prototyping:** Describe a pricing page or a dashboard layout, and Accuretta will generate it while rendering the result natively in the application browser.
* **System Management:** Ask the agent to clean up your download folder or look for specific logs. It will write the necessary PowerShell commands and wait for your permission to execute them.
* **Mobile Companion:** Access the interface from your phone on the local network to control your desktop remotely or chat with your local models while away from your keyboard.
* **Workspace Analysis:** You can grant the application access to a specific workspace folder and ask the agent to act as a localized coding assistant. For example, simply ask, "I am having issues with the Python script in the workspace, can you help?" The agent will read the relevant files and propose fixes. Naturally, the execution quality will vary depending on your chosen model, but the foundational capability is completely supported.

### Alternative Perspectives

If you are evaluating Accuretta alongside tools like Claude Computer Use, OpenClaw, or the Hermes agent, it is helpful to view them as complementary alternatives designed with different priorities in mind. 

* **Complete Locality:** Cloud based computing agents rely on external enterprise AI models, which inherently require sending screenshots and commands over the internet. Accuretta is firmly rooted in your local machine securely tied to Ollama. No data ever leaves your computer, ensuring absolute zero trust privacy.
* **Integrated Web Rendering:** While most agents focus primarily on executing tasks in terminal or browser windows, Accuretta provides a built in IDE. It acts as an ambient assistant but will instantly spawn a live preview window whenever you ask it to generate interface code. You can iterate on designs directly inside the application.
* **Explicit Permission Guardrails:** Many modern orchestration tools run in fully autonomous loops, allowing the model to freely interact with the operating system until a task is completed. Accuretta takes a fundamentally defensive approach. Every single file modification, script execution, and program launch is pushed to a pending queue. Nothing alters your system without your distinct manual approval.
* **Model Capability Differences:** It is important to acknowledge that Accuretta utilizes smaller open weight vision models rather than massive cloud intelligence. As a result, its spatial reasoning and desktop navigation might be less refined than proprietary enterprise counterparts. It prioritizes absolute privacy and safety over raw unrestrained automation capabilities.

### Setup and Usage

* Ensure you have Python installed on your system.
* Ensure Ollama is installed and running with at least one model pulled to your machine.
* Run the startup batch file. It will automatically clear the required network port, start the Python bridge, and launch the user interface in your default browser.
* Inside the settings panel, you can configure your context window size, select the active chat model, and toggle the desktop vision capabilities.
* You connect to this application from your mobile phone through Tailscale, meaning we have left the authentication completely up to that app for security. Just get the desktop IP address from Tailscale, type it into the address bar in your phone browser along with the port and `/index.html` to see the interface.

**Connection Overview:**

```text
+================+                         +=================+
|  Mobile Phone  |                         | Desktop Machine |
|                |      Tailscale VPN      |                 |
| Phone Browser  | == secure connection >> | Accuretta App   |
| 100.x.x.x:8787 |                         | Port 8787       |
+================+                         +=================+
```

### Known Issues

* Desktop agent is in its first iteration, expect it not to work fully yet. It may misinterpret screen coordinates or fail to execute complex sequential inputs accurately.
* Context window overflow may cause older messages to drop abruptly if the token limit is set too low for your chosen model.
* Most of the execution is model dependent. Sometimes smaller parameter models take instructions literally when asked to execute tasks on the PC, so your mileage may vary.
* Modelfiles sometimes need to be altered if models are downloaded from HuggingFace to get the proper chat template due to uploaders not setting that up. As an interesting note, the modelfiles included in this repository were actually generated by Accuretta itself. When early testing ran into response formatting issues with raw models, the agent was simply provided with the directory path to the downloaded files. It autonomously diagnosed the missing chat templates and drafted the correct configuration files to resolve the problems.

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
* Modelfiles sometimes need to be altered if models are downloaded from HuggingFace to get the proper chat template due to uploaders not setting that up.

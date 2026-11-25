/* SPDX-License-Identifier: AGPL-3.0-only */
/* tool icons (inlined so no extra HTTP / static-whitelist changes): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["tool-icons"] = ({ esc, state }) => {
  const TOOL_SVG = {
    searching_computer: '<svg viewBox="0 0 150.817 150.817" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M58.263,64.946c3.58-8.537,9.834-16.039,18.456-21.02c6.644-3.842,14.225-5.876,21.902-5.876c6.376,0,12.568,1.461,18.207,4.031V21.677C116.829,9.706,92.563,0,62.641,0C32.71,0,8.448,9.706,8.448,21.677v21.681C8.436,54.75,30.372,64.061,58.263,64.946z M62.629,5.416c29.77,0,48.768,9.633,48.768,16.255c0,6.634-18.998,16.258-48.768,16.258c-29.776,0-48.774-9.624-48.774-16.258C13.855,15.049,32.853,5.416,62.629,5.416z M8.429,75.883V54.202c0,10.973,20.396,20.015,46.841,21.449c-1.053,7.21-0.311,14.699,2.375,21.799C30.055,96.445,8.436,87.184,8.429,75.883z M95.425,125.631c-9.109,2.771-20.457,4.445-32.796,4.445c-29.931,0-54.193-9.706-54.193-21.684V86.709c0,11.983,24.256,21.684,54.193,21.684c0.341,0,0.673-0.018,1.014-0.018C71.214,118.373,82.827,124.656,95.425,125.631z M131.296,63.11c-10.388-17.987-33.466-24.174-51.46-13.785c-17.987,10.388-24.173,33.463-13.792,51.45c10.388,17.993,33.478,24.174,51.465,13.798C135.51,104.191,141.684,81.102,131.296,63.11z M71.449,97.657C62.778,82.66,67.945,63.394,82.955,54.72c15.01-8.662,34.275-3.504,42.946,11.509c8.672,15.013,3.502,34.279-11.508,42.943C99.377,117.85,80.117,112.686,71.449,97.657z M139.456,133.852l-16.203,9.353l-12.477-21.598l16.209-9.359L139.456,133.852z M137.708,149.562c-4.488,2.582-10.199,1.06-12.794-3.429l16.216-9.353C143.718,141.268,142.184,146.979,137.708,149.562z"/></svg>',
    computer_search_failed: '<svg viewBox="0 0 139.558 139.558" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M54.19,65.705c29.938,0,54.19-9.703,54.19-21.681V22.344c0-11.977-24.265-21.68-54.19-21.68C24.263,0.664,0,10.367,0,22.344v21.681C0,56.002,24.256,65.705,54.19,65.705z M54.19,6.089c29.773,0,48.771,9.627,48.771,16.255c0,6.628-18.998,16.262-48.771,16.262c-29.772,0-48.771-9.627-48.771-16.255C5.419,15.722,24.418,6.089,54.19,6.089z"/><path d="M54.19,98.225c6.467,0,12.638-0.476,18.39-1.304c4.643-15.381,18.928-26.609,35.801-26.609V54.866c0,11.971-24.265,21.681-54.19,21.681C24.263,76.547,0,66.844,0,54.866v21.681C0,88.518,24.256,98.225,54.19,98.225z"/><path d="M54.19,109.057c-29.934,0-54.19-9.7-54.19-21.678v21.678c0,11.978,24.263,21.684,54.19,21.684c8.306,0,16.148-0.779,23.19-2.107c-3.997-5.906-6.342-13.006-6.394-20.648C65.696,108.673,60.058,109.057,54.19,109.057z"/><path d="M108.381,76.541c-17.214,0-31.176,13.962-31.176,31.176c0,17.215,13.962,31.177,31.176,31.177s31.177-13.962,31.177-31.177C139.558,90.503,125.595,76.541,108.381,76.541z M122.709,115.432l-6.613,6.613l-7.715-7.709l-7.715,7.709l-6.612-6.613l7.708-7.715l-7.708-7.715l6.612-6.613l7.715,7.722l7.715-7.722l6.613,6.613L115,107.717L122.709,115.432z"/></svg>',
    writing_file: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M17.093,1.293l-11.2,11.2a.99.99,0,0,0-.242.391l-1.6,4.8A1,1,0,0,0,5,19a1.014,1.014,0,0,0,.316-.051l4.8-1.6a1.006,1.006,0,0,0,.391-.242l11.2-11.2a1,1,0,0,0,0-1.414l-3.2-3.2A1,1,0,0,0,17.093,1.293ZM9.26,15.526l-2.679.893.893-2.679L17.8,3.414,19.586,5.2ZM3,21H20a1,1,0,0,1,0,2H3a1,1,0,0,1,0-2Z"/></svg>',
    editing_file: '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5,9.2H9.2v0H35.1a3.9828,3.9828,0,0,1,3.7,3.7l.1123,20.7359"/><path d="M9.281,13.7433,9.2,35.1a3.9807,3.9807,0,0,0,3.7,3.7H38.8v0h3.7"/><path d="M16.6,31.4V27.7L27.7,16.6l3.7,3.7L20.3,31.4Z"/></svg>',
    deleted: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path fill-rule="evenodd" clip-rule="evenodd" d="M10.3094 2.25002H13.6908C13.9072 2.24988 14.0957 2.24976 14.2737 2.27819C14.977 2.39049 15.5856 2.82915 15.9146 3.46084C15.9978 3.62073 16.0573 3.79961 16.1256 4.00494L16.2373 4.33984C16.2562 4.39653 16.2616 4.41258 16.2661 4.42522C16.4413 4.90933 16.8953 5.23659 17.4099 5.24964C17.4235 5.24998 17.44 5.25004 17.5001 5.25004H20.5001C20.9143 5.25004 21.2501 5.58582 21.2501 6.00004C21.2501 6.41425 20.9143 6.75004 20.5001 6.75004H3.5C3.08579 6.75004 2.75 6.41425 2.75 6.00004C2.75 5.58582 3.08579 5.25004 3.5 5.25004H6.50008C6.56013 5.25004 6.5767 5.24998 6.59023 5.24964C7.10488 5.23659 7.55891 4.90936 7.73402 4.42524C7.73863 4.41251 7.74392 4.39681 7.76291 4.33984L7.87452 4.00496C7.94281 3.79964 8.00233 3.62073 8.08559 3.46084C8.41453 2.82915 9.02313 2.39049 9.72643 2.27819C9.90445 2.24976 10.093 2.24988 10.3094 2.25002ZM9.00815 5.25004C9.05966 5.14902 9.10531 5.04404 9.14458 4.93548C9.1565 4.90251 9.1682 4.86742 9.18322 4.82234L9.28302 4.52292C9.37419 4.24941 9.39519 4.19363 9.41601 4.15364C9.52566 3.94307 9.72853 3.79686 9.96296 3.75942C10.0075 3.75231 10.067 3.75004 10.3553 3.75004H13.6448C13.9331 3.75004 13.9927 3.75231 14.0372 3.75942C14.2716 3.79686 14.4745 3.94307 14.5842 4.15364C14.605 4.19363 14.626 4.2494 14.7171 4.52292L14.8169 4.82216L14.8556 4.9355C14.8949 5.04405 14.9405 5.14902 14.992 5.25004H9.00815Z"/><path d="M5.91509 8.45015C5.88754 8.03685 5.53016 7.72415 5.11686 7.7517C4.70357 7.77925 4.39086 8.13663 4.41841 8.54993L4.88186 15.5017C4.96736 16.7844 5.03642 17.8205 5.19839 18.6336C5.36679 19.4789 5.65321 20.185 6.2448 20.7385C6.8364 21.2919 7.55995 21.5308 8.4146 21.6425C9.23662 21.7501 10.275 21.7501 11.5606 21.75H12.4395C13.7251 21.7501 14.7635 21.7501 15.5856 21.6425C16.4402 21.5308 17.1638 21.2919 17.7554 20.7385C18.347 20.185 18.6334 19.4789 18.8018 18.6336C18.9638 17.8206 19.0328 16.7844 19.1183 15.5017L19.5818 8.54993C19.6093 8.13663 19.2966 7.77925 18.8833 7.7517C18.47 7.72415 18.1126 8.03685 18.0851 8.45015L17.6251 15.3493C17.5353 16.6971 17.4713 17.6349 17.3307 18.3406C17.1943 19.025 17.004 19.3873 16.7306 19.6431C16.4572 19.8989 16.083 20.0647 15.391 20.1552C14.6776 20.2485 13.7376 20.25 12.3868 20.25H11.6134C10.2626 20.25 9.32255 20.2485 8.60915 20.1552C7.91715 20.0647 7.54299 19.8989 7.26958 19.6431C6.99617 19.3873 6.80583 19.025 6.66948 18.3406C6.52892 17.6349 6.46489 16.6971 6.37503 15.3493L5.91509 8.45015Z"/><path d="M9.42546 10.2538C9.83762 10.2125 10.2052 10.5133 10.2464 10.9254L10.7464 15.9254C10.7876 16.3376 10.4869 16.7051 10.0747 16.7463C9.66256 16.7875 9.29503 16.4868 9.25381 16.0747L8.75381 11.0747C8.7126 10.6625 9.01331 10.295 9.42546 10.2538Z"/><path d="M14.5747 10.2538C14.9869 10.295 15.2876 10.6625 15.2464 11.0747L14.7464 16.0747C14.7052 16.4868 14.3376 16.7875 13.9255 16.7463C13.5133 16.7051 13.2126 16.3376 13.2538 15.9254L13.7538 10.9254C13.795 10.5133 14.1626 10.2125 14.5747 10.2538Z"/></svg>',
    running_command: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 17H20"/><path d="M5 7L10 12L5 17"/></svg>',
    command_failed: '<svg viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><rect x="17" y="23" width="6" height="2"/><polygon points="7 24.11 16.6 19.7 16.6 17.89 7 13.48 7 15.68 13.79 18.8 7 21.91 7 24.11"/><path d="M33.68,15.4H32V29H4V10.8H18.68A3.66,3.66,0,0,1,19,9.89l.4-.69H4V7H20.71l1.15-2H4A2,2,0,0,0,2,7V29a2,2,0,0,0,2,2H32a2,2,0,0,0,2-2V15.38Z"/><path d="M26.85,1.14,21.13,11A1.28,1.28,0,0,0,22.23,13H33.68A1.28,1.28,0,0,0,34.78,11L29.06,1.14A1.28,1.28,0,0,0,26.85,1.14Z"/></svg>',
    downloading_file: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11C3 11.9319 3 12.3978 3.15224 12.7654C3.35523 13.2554 3.74458 13.6448 4.23463 13.8478C4.60218 14 5.06812 14 6 14H6.67544C7.25646 14 7.54696 14 7.77888 14.1338C7.83745 14.1675 7.89245 14.2072 7.94303 14.2521C8.14326 14.4298 8.23513 14.7054 8.41886 15.2566L8.54415 15.6325C8.76416 16.2925 8.87416 16.6225 9.13605 16.8112C9.39794 17 9.7458 17 10.4415 17H13.5585C14.2542 17 14.6021 17 14.864 16.8112C15.1258 16.6225 15.2358 16.2925 15.4558 15.6325L15.5811 15.2566C15.7649 14.7054 15.8567 14.4298 16.057 14.2521C16.1075 14.2072 16.1625 14.1675 16.2211 14.1338C16.453 14 16.7435 14 17.3246 14H18C18.9319 14 19.3978 14 19.7654 13.8478C20.2554 13.6448 20.6448 13.2554 20.8478 12.7654C21 12.3978 21 11.9319 21 11"/><path d="M8 9L12 12M12 12L16 9M12 12L12 2"/><path d="M16 5H17C18.8856 5 19.8284 5 20.4142 5.58579C21 6.17157 21 7.11438 21 9V17C21 18.8856 21 19.8284 20.4142 20.4142C19.8284 21 18.8856 21 17 21H7C5.11438 21 4.17157 21 3.58579 20.4142C3 19.8284 3 18.8856 3 17V9C3 7.11438 3 6.17157 3.58579 5.58579C4.17157 5 5.11438 5 7 5H8"/></svg>',
    multiple_tasks_complete: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8.00009 13L12.2278 16.3821C12.6557 16.7245 13.2794 16.6586 13.6264 16.2345L22.0001 6"/><path d="M9.6434 11.5995L14.5356 5.6201"/><path d="M2.36 13.52L4.87309 16.9049C5.559 17.4193 6.52849 17.3016 7.07142 16.638L8.03225 15.4637"/></svg>',
    globe: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
    // YARA: shield-with-magnifier. The shield is the rule set, the lens is
    // matching — together it reads "scanning for known-bad patterns".
    yara_scanning: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 4.5 3.2 8.5 8 10 1.4-.45 2.7-1.1 3.8-1.95"/><circle cx="16.5" cy="14.5" r="3.2"/><path d="m18.9 16.9 2.6 2.6"/></svg>',
    yara_failed: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 4.5 3.2 8.5 8 10 4.8-1.5 8-5.5 8-10V5z"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
    // binary_inspect: microchip with pin legs. Reads as "looking inside a
    // compiled artifact" without leaning on a generic file glyph.
    chip_inspecting: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/><rect x="9.5" y="9.5" width="5" height="5" rx="0.6"/><path d="M9 3v3M12 3v3M15 3v3M9 18v3M12 18v3M15 18v3M3 9h3M3 12h3M3 15h3M18 9h3M18 12h3M18 15h3"/></svg>',
    chip_failed: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="m9.5 9.5 5 5M14.5 9.5l-5 5"/><path d="M9 3v3M12 3v3M15 3v3M9 18v3M12 18v3M15 18v3M3 9h3M3 12h3M3 15h3M18 9h3M18 12h3M18 15h3"/></svg>',
  };
  const TOOL_ICON_MAP = {
    list_directory:  { run: "searching_computer", err: "computer_search_failed" },
    read_file:       { run: "searching_computer", err: "computer_search_failed" },
    grep_files:      { run: "searching_computer", err: "computer_search_failed" },
    write_file:      { run: "writing_file",       err: "command_failed" },
    edit_file:       { run: "editing_file",       err: "command_failed" },
    patch_file:      { run: "editing_file",       err: "command_failed" },
    delete_file:     { run: "deleted",            err: "command_failed" },
    run_powershell:  { run: "running_command",    err: "command_failed" },
    open_program:    { run: "running_command",    err: "command_failed" },
    web_fetch:       { run: "downloading_file",   err: "command_failed" },
    web_search:      { run: "globe",              err: "computer_search_failed" },
    network_snapshot:{ run: "globe",              err: "computer_search_failed" },
    yara_scan:       { run: "yara_scanning",      err: "yara_failed" },
    binary_inspect:  { run: "chip_inspecting",    err: "chip_failed" },
  };

  const APPROVAL_SVG = {
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 4.5 6v5.4c0 4.5 2.8 7.9 7.5 9.6 4.7-1.7 7.5-5.1 7.5-9.6V6z"/><path d="m9 12 2 2 4-4"/></svg>',
    git: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7" cy="5" r="2"/><circle cx="17" cy="19" r="2"/><path d="M7 7v5a7 7 0 0 0 7 7h1M17 5v7"/><path d="m14 9 3 3 3-3"/></svg>',
    git_push: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="18" r="2"/><path d="M6 16V9a4 4 0 0 1 4-4h3M14 18h4V7"/><path d="m14 11 4-4 4 4"/></svg>',
    git_pull: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="2"/><path d="M6 8v7a4 4 0 0 0 4 4h3M18 5v12"/><path d="m14 13 4 4 4-4"/></svg>',
    git_commit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h5m8 0h5"/><circle cx="12" cy="12" r="4"/></svg>',
    git_merge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="12" cy="19" r="2"/><path d="M6 7v2a6 6 0 0 0 6 6v2M18 7v2a6 6 0 0 1-6 6"/></svg>',
    git_branch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M8 13h3a7 7 0 0 0 7-4"/></svg>',
    git_reset: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8V4m0 0h4M5 5a9 9 0 1 1-1.5 9"/><circle cx="12" cy="12" r="2"/></svg>',
    git_add: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 4h9l5 5v11H5z"/><path d="M14 4v5h5M9 14h6M12 11v6"/></svg>',
    git_restore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 4h8l4 4v12H7zM15 4v4h4"/><path d="M10 12H7m0 0 2-2m-2 2 2 2M8 12a5 5 0 0 1 8 4"/></svg>',
    git_init: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M13 8h8M17 4v8"/></svg>',
    git_clone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M16 4v12m-4-4 4 4 4-4"/></svg>',
    test: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3h6M10 3v5l-5 9a2.7 2.7 0 0 0 2.4 4h9.2a2.7 2.7 0 0 0 2.4-4l-5-9V3"/><path d="M7.8 15h8.4"/></svg>',
    launch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 8h18"/><path d="m10 11 5 3-5 3z"/></svg>',
    registry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/><path d="m12 14 2-2 2 2m-2-2v5"/></svg>',
    desktop_click: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 3 13 9-6 1.5L9 19z"/><path d="m13 14 4 6"/></svg>',
    desktop_type: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10h.01M11 10h.01M15 10h.01M18 10h.01M7 14h.01M10 14h7"/></svg>',
    desktop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
    archive: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16v13H4zM3 3h18v4H3z"/><path d="M9 11h6M12 11v5m-2-2 2 2 2-2"/></svg>',
    event_log: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 4h6l1 2h3v15H5V6h3z"/><path d="M8 11h8M8 15h8M8 18h5"/></svg>',
    persistence: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m16 16 5 5M11 7v4l3 2"/></svg>',
    mcp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3v5M16 3v5M6 8h12v3a6 6 0 0 1-6 6v4"/></svg>',
    skill: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4h11a3 3 0 0 1 3 3v13H7a3 3 0 0 1-3-3z"/><path d="M7 16h11M15 3v6l-2-1-2 1V3"/></svg>',
  };

  function approvalIconSvg(approval) {
    const details = approval?.details || {};
    const kind = String(details.kind || "").toLowerCase();
    if (kind === "git") {
      const verb = String(details.argv?.[0] || "").toLowerCase();
      const gitKey = ({
        push: "git_push", pull: "git_pull", fetch: "git_pull",
        commit: "git_commit", merge: "git_merge", branch: "git_branch",
        checkout: "git_branch", switch: "git_branch", reset: "git_reset",
        revert: "git_reset", add: "git_add", restore: "git_restore",
        init: "git_init", clone: "git_clone",
      })[verb];
      return APPROVAL_SVG[gitKey] || APPROVAL_SVG.git;
    }
    if (kind.startsWith("desktop.")) {
      const action = kind.slice("desktop.".length);
      if (action === "click") return APPROVAL_SVG.desktop_click;
      if (action === "type" || action === "keys") return APPROVAL_SVG.desktop_type;
      return APPROVAL_SVG.desktop;
    }
    if (kind.startsWith("desktop_")) {
      if (kind.includes("click")) return APPROVAL_SVG.desktop_click;
      if (kind.includes("type") || kind.includes("keys")) return APPROVAL_SVG.desktop_type;
      return APPROVAL_SVG.desktop;
    }
    if (kind.startsWith("mcp_")) return APPROVAL_SVG.mcp;

    const icon = ({
      write_file: TOOL_SVG.writing_file,
      edit_file: TOOL_SVG.editing_file,
      replace_ast_node: TOOL_SVG.editing_file,
      delete: TOOL_SVG.deleted,
      powershell: TOOL_SVG.running_command,
      run_powershell: TOOL_SVG.running_command,
      command: TOOL_SVG.running_command,
      session: TOOL_SVG.running_command,
      sandbox: TOOL_SVG.running_command,
      remote_shell: TOOL_SVG.running_command,
      test: APPROVAL_SVG.test,
      launch: APPROVAL_SVG.launch,
      registry: APPROVAL_SVG.registry,
      remote_file_write: TOOL_SVG.writing_file,
      remote_file_copy: APPROVAL_SVG.copy,
      network_snapshot: TOOL_SVG.globe,
      event_log: APPROVAL_SVG.event_log,
      persistence_hunt: APPROVAL_SVG.persistence,
      extract_archive: APPROVAL_SVG.archive,
      extract_squashfs: APPROVAL_SVG.archive,
      carve_file: TOOL_SVG.writing_file,
      decompile_apk: TOOL_SVG.chip_inspecting,
      save_skill: APPROVAL_SVG.skill,
      recon_s3: TOOL_SVG.globe,
    })[kind];
    return icon || APPROVAL_SVG.shield;
  }

  function renderWebSearchChips(results) {
    if (!results || !results.length) return "";
    const max = 4;
    const visible = results.slice(0, max);
    const overflow = results.length - visible.length;
    const chips = visible.map(r => {
      let host = "";
      try { host = new URL(r.url).hostname.replace(/^www\./, ""); } catch {}
      const label = host || r.url;
      const fav = host && !state.settings.offline_active ? `https://icons.duckduckgo.com/ip3/${host}.ico` : "";
      // Wrap: globe fallback always rendered; img sits on top, hides itself on error.
      const favHtml = `<span class="web-fav-wrap">${TOOL_SVG.globe}${fav ? `<img class="web-fav" src="${esc(fav)}" alt="" loading="lazy" onerror="this.style.display='none'">` : ""}</span>`;
      return `<a class="web-chip" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer" title="${esc(r.title || r.url)}">${favHtml}<span>${esc(label)}</span></a>`;
    }).join("");
    const moreChip = overflow > 0
      ? `<span class="web-chip web-chip-more">+${overflow} more</span>`
      : "";
    return `<div class="web-results">${chips}${moreChip}</div>`;
  }

  function renderNetworkChart(res) {
    if (!res || res.error) return "";
    const tcp = res.tcp_count || 0;
    const udp = res.udp_count || 0;
    const dns = (res.recent_dns || []).length;
    const procs = (res.top_processes || []).slice(0, 6);
    const remotes = (res.top_remotes || []).slice(0, 6);
    // Max is at least 1 (no divide by zero) and widths clamp to [4, 100]; the
    // bars depend only on the data.
    const procMax = Math.max(1, ...procs.map(p => p.connections || 0));
    const remMax  = Math.max(1, ...remotes.map(r => r.count || 0));
    const pct = (val, max) => Math.max(4, Math.min(100, Math.round((val / max) * 100)));
    const stat = (label, n, cls) => `<div class="net-stat ${cls}"><div class="net-stat-num">${n}</div><div class="net-stat-lbl">${label}</div></div>`;
    const procRow = (p) => {
      const v = p.connections || 0;
      const w = pct(v, procMax);
      return `<div class="net-bar-row"><span class="net-bar-lbl" title="${esc(p.process || "?")}">${esc(p.process || "?")}</span><span class="net-bar-track" data-v="${v}" data-max="${procMax}"><span class="net-bar-fill net-bar-proc" style="width:${w}%"></span></span><span class="net-bar-num">${v}</span></div>`;
    };
    const remRow = (r) => {
      const v = r.count || 0;
      const w = pct(v, remMax);
      const lbl = `${r.address || "?"}${r.port ? ":" + r.port : ""}`;
      return `<div class="net-bar-row"><span class="net-bar-lbl" title="${esc(lbl)}">${esc(lbl)}</span><span class="net-bar-track" data-v="${v}" data-max="${remMax}"><span class="net-bar-fill net-bar-rem" style="width:${w}%"></span></span><span class="net-bar-num">${v}</span></div>`;
    };
    const procBlock = procs.length
      ? `<div class="net-block"><div class="net-block-title">Top processes</div>${procs.map(procRow).join("")}</div>`
      : "";
    const remBlock = remotes.length
      ? `<div class="net-block"><div class="net-block-title">Top remote endpoints</div>${remotes.map(remRow).join("")}</div>`
      : "";
    return `<div class="netscan-card">
      <div class="net-stats">${stat("TCP", tcp, "net-stat-tcp")}${stat("UDP", udp, "net-stat-udp")}${stat("DNS", dns, "net-stat-dns")}</div>
      ${procBlock}${remBlock}
    </div>`;
  }

  function toolIconHtml(name, kind /* "run" | "done" | "err" */) {
    const map = TOOL_ICON_MAP[name];
    if (!map) return null;
    const which = kind === "err" ? map.err : map.run;
    const svg = TOOL_SVG[which];
    if (!svg) return null;
    const cls = `tool-svg ${kind === "run" ? "breathing" : kind === "err" ? "is-err" : "is-done"}`;
    return `<span class="${cls}">${svg}</span>`;
  }
  return { TOOL_SVG, TOOL_ICON_MAP, approvalIconSvg, renderWebSearchChips, renderNetworkChart };
};

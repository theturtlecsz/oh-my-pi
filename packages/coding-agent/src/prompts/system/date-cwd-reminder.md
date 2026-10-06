<system-reminder>
Today: {{date}}; current working directory: '{{cwd}}'. Do not repeat this information in your reply.
{{#if stageContext}}

Stage context compiled for this worker at launch:
{{stageContext}}
{{/if}}
</system-reminder>

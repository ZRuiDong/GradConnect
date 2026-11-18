from django import forms


class DropdownCheckboxSelect(forms.CheckboxSelectMultiple):
    template_name = "widgets/dropdown_checkbox_select.html"
    option_template_name = "widgets/dropdown_checkbox_option.html"

    def __init__(self, *args, label="", placeholder="不限", **kwargs):
        self.filter_label = label
        self.placeholder = placeholder
        super().__init__(*args, **kwargs)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        selected = [str(option["label"]) for _, options, _ in context["widget"]["optgroups"]
                    for option in options if option["selected"]]
        context.update(filter_label=self.filter_label, placeholder=self.placeholder, selection_count=len(selected),
                       selection_text=(selected[0] + (f" 等{len(selected)}项" if len(selected) > 1 else "")) if selected else self.placeholder)
        return context

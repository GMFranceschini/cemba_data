import seaborn as sns


def continuous_color_palette(color, n, skip_border=1):
    """
    This function concatenate the result of both sns.light_palette
    and sns.dark_palette to get a wider color range
    """
    if n == 1:
        return [color]
    if n < 1:
        raise ValueError('parameter n colors must >= 1.')

    # this is just trying to make sure len(color) == n
    light_n = (n + 2 * skip_border) // 2
    light_colors = sns.light_palette(color, n_colors=light_n)[skip_border:]
    dark_n = n + 2 * skip_border - light_n + 1
    dark_colors = sns.dark_palette(color, n_colors=dark_n, reverse=True)[1:-skip_border]
    colors = light_colors + dark_colors
    return colors


def level_one_palette(name_list, order=None, palette='default'):
    if palette == 'default':
        if len(set(name_list)) < 10:
            palette = 'tab10'
        else:
            palette = 'tab20'

    if order is None:
        order = set(name_list)
    else:
        if set(order) != set(name_list):
            raise ValueError('Order is not equal to set(name_list).')
    n = len(set(name_list))
    colors = sns.color_palette(palette, n)
    color_palette = {}
    for name, color in zip(order, colors):
        color_palette[name] = color
    return color_palette


def level_two_palette(major_color, major_sub_dict,
                      major_order=None, palette='default',
                      skip_border_color=2):
    if isinstance(major_color, list):
        if len(major_color) > 20:
            print(f'Warning: too much major color {len(major_color)} is not distinguishable. '
                  f'Color will repeat.')
        major_color_dict = level_one_palette(major_color, palette=palette, order=major_order)
    else:
        major_color_dict = major_color

    sub_id_list = []
    for subs in major_sub_dict.values():
        sub_id_list += list(subs)
    if len(sub_id_list) != len(set(sub_id_list)):
        raise ValueError('Sub id in the major_dub_dict is not unique.')

    color_palette = {}
    for major, color in major_color_dict.items():
        subs = major_sub_dict[major]
        n = len(subs)
        colors = continuous_color_palette(color, n, skip_border=skip_border_color)
        for sub, _color in zip(subs, colors):
            color_palette[sub] = _color
    return color_palette